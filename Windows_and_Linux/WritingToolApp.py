import gettext
import json
import logging
import os
import signal
import sys
import threading
import time

import darkdetect
import pyperclip
import ui.AboutWindow
import ui.CustomPopupWindow
import ui.OnboardingWindow
import ui.ResponseWindow
import ui.SettingsWindow
import ui.VariationPreviewWindow
from ui.UIUtils import get_resource_path
from aiprovider import GeminiProvider, OllamaProvider, OpenAICompatibleProvider, obfuscate_api_key, parse_variations_response
from pynput import keyboard as pykeyboard
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QLocale, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox
from update_checker import UpdateChecker

_ = gettext.gettext


class _SelectedTextHolder:
    """
    Carries the result of an async clipboard capture from `_show_popup` to
    `process_option_thread`. The capture thread sets `text` and signals
    `ready` once done.
    """
    __slots__ = ("text", "ready")

    def __init__(self):
        self.text = ""
        self.ready = threading.Event()


class WritingToolApp(QtWidgets.QApplication):
    """
    The main application class for Writing Tools.
    """
    output_ready_signal = Signal(str)
    show_message_signal = Signal(str, str)  # a signal for showing message boxes
    hotkey_triggered_signal = Signal()
    followup_response_signal = Signal(str)
    show_variation_preview_signal = Signal(str, list)
    show_variation_loading_signal = Signal(str)   # shows window immediately with loading state
    update_preview_variations_signal = Signal(list)


    def __init__(self, argv):
        super().__init__(argv)
        self.current_response_window = None
        self.current_preview_window = None
        logging.debug('Initializing WritingToolApp')
        self.output_ready_signal.connect(self.replace_text)
        self.show_message_signal.connect(self.show_message_box)
        self.hotkey_triggered_signal.connect(self.on_hotkey_pressed)
        self.show_variation_preview_signal.connect(self._show_variation_preview_window)
        self.show_variation_loading_signal.connect(self._show_variation_loading_window)
        self.update_preview_variations_signal.connect(self._update_current_preview_variations)
        self.config = None
        self.config_path = None
        self.load_config()

        # Run any pending config migrations in a single pass (single restart).
        self._migrate_config()

        self.options = None
        self.options_path = None
        self.load_options()
        self.onboarding_window = None
        self.popup_window = None
        self.tray_icon = None
        self.tray_menu = None
        self.settings_window = None
        self.about_window = None
        self.registered_hotkey = None
        self.output_queue = ""
        self.last_replace = 0
        self.hotkey_listener = None
        self.paused = False
        self.toggle_action = None
        self.last_active_window = None

        # Holder for the user's selected text. Populated asynchronously by a
        # background thread so the popup can show instantly — see `_show_popup`
        # and `_fire_ctrl_c_and_capture_async`. The lock serializes Ctrl+C
        # captures across rapid hotkey presses so two captures don't fight
        # over the clipboard at once.
        self.current_text_holder = None
        self._capture_lock = threading.Lock()

        self._ = gettext.gettext

        # Initialize the ctrl+c hotkey listener
        self.ctrl_c_timer = None
        self.setup_ctrl_c_listener()

        # Setup available AI providers
        self.providers = [GeminiProvider(self), OpenAICompatibleProvider(self), OllamaProvider(self)]

        if not self.config:
            logging.debug('No config found, showing onboarding')
            self.show_onboarding()
        else:
            logging.debug('Config found, setting up hotkey and tray icon')

            # Initialize the current provider, defaulting to Gemini
            provider_name = self.config.get('provider', 'Gemini')

            self.current_provider = next((provider for provider in self.providers if provider.provider_name == provider_name), None)
            if not self.current_provider:
                logging.warning(f'Provider {provider_name} not found. Using default provider.')
                self.current_provider = self.providers[0]

            self.current_provider.load_config(self.config.get("providers", {}).get(provider_name, {}))

            self.create_tray_icon()
            self.register_hotkey()

            try:
                lang = self.config['locale']
            except KeyError:
                lang = None
            self.change_language(lang)

            # Initialize update checker
            self.update_checker = UpdateChecker(self)
            self.update_checker.check_updates_async()

        self.recent_triggers = []  # Track recent hotkey triggers
        self.TRIGGER_WINDOW = 1.5  # Time window in seconds
        self.MAX_TRIGGERS = 3  # Max allowed triggers in window

    def setup_translations(self, lang=None):
        if not lang:
            lang = QLocale.system().name().split('_')[0]

        try:
            translation = gettext.translation(
                'messages',
                localedir=get_resource_path('locales'),
                languages=[lang]
            )
        except FileNotFoundError:
            translation = gettext.NullTranslations()

        translation.install()
        # Update the translation function for all UI components.
        self._ = translation.gettext
        ui.AboutWindow._ = self._
        ui.SettingsWindow._ = self._
        ui.ResponseWindow._ = self._
        ui.OnboardingWindow._ = self._
        ui.CustomPopupWindow._ = self._

    def retranslate_ui(self):
        self.update_tray_menu()

    def change_language(self, lang):
        self.setup_translations(lang)
        self.retranslate_ui()

        # Update all other windows
        for widget in QApplication.topLevelWidgets():
            if widget != self and hasattr(widget, 'retranslate_ui'):
                widget.retranslate_ui()

    def check_trigger_spam(self):
        """
        Check if hotkey is being triggered too frequently (3+ times in 1.5 seconds).
        Returns True if spam is detected.
        """
        current_time = time.time()
        
        # Add current trigger
        self.recent_triggers.append(current_time)
        
        # Remove old triggers outside the window
        self.recent_triggers = [t for t in self.recent_triggers 
                            if current_time - t <= self.TRIGGER_WINDOW]
        
        # Check if we have too many triggers in the window
        return len(self.recent_triggers) >= self.MAX_TRIGGERS

    def load_config(self):
        """
        Load the configuration file.
        """
        self.config_path = os.path.join(os.path.dirname(sys.argv[0]), 'config.json')
        logging.debug(f'Loading config from {self.config_path}')
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
                logging.debug('Config loaded successfully')
        else:
            logging.debug('Config file not found')
            self.config = None

    def _migrate_config(self):
        """
        One-shot config migration. Catches any user up to the current schema
        (v9) regardless of where they started — v7, v8, or already current —
        in a single pass with at most one restart.

        Each version is gated on its own `is_config_file_updated_for_v{N}`
        flag, so re-running this is a no-op once everything's caught up.
        Bump CURRENT_CONFIG_VERSION and add a `# v{N}` block when adding a
        new migration step.

        v8 (introduced 2025):
          • Google removed Gemini 2.0 from the free API → bump model.
          • Obfuscate plaintext Gemini API keys (defeats Ctrl+F scanning).
          The custom-model input field didn't exist pre-v8, so any pre-v8
          model value is safe to overwrite — there's nothing custom to
          preserve.

        v9 (introduced 2026):
          • Google deprecated the Gemma 3 family. Migrate every v8 user on a
            now-deprecated preset to the new default (`gemini-flash-latest`)
            so they immediately land on the fast experience. They can opt
            into the unlimited-but-slow Gemma 4 options from the dropdown
            if they hit the 20/day Flash quota.
            Preserve custom model values (which DID exist by then) so a user
            who picked, say, `gemini-3.1-pro-preview` doesn't get reset.
          • SDK migration to `google-genai` is code-side only; nothing to
            do in config.
        """
        CURRENT_CONFIG_VERSION = 9
        # Default for new installs and migrating users.
        NEW_DEFAULT_MODEL = 'gemini-3.1-flash-lite'
        # v8 -> v9 model mapping. Every retired preset is bumped to the new
        # default so users get the fast Flash-tier experience by default.
        V8_TO_V9_MAP = {
            'gemma-3-27b-it':           NEW_DEFAULT_MODEL,
            'gemma-3-4b-it':            NEW_DEFAULT_MODEL,
            'gemini-flash-lite-latest': NEW_DEFAULT_MODEL,
            # 'gemini-flash-latest' itself is already current — no entry needed.
        }

        # New user (no config yet) — onboarding will create a fresh, current
        # config; nothing to migrate.
        if not self.config:
            logging.debug('No config to migrate (new user)')
            return

        needs_v8 = not self.config.get('is_config_file_updated_for_v8', False)
        needs_v9 = not self.config.get('is_config_file_updated_for_v9', False)

        if not needs_v8 and not needs_v9:
            logging.debug('Config already up-to-date, no migration needed')
            return

        logging.info(f'Running config migration (needs_v8={needs_v8}, needs_v9={needs_v9})...')

        config_changed = False
        gemini_config = (
            self.config.get('providers', {}).get('Gemini (Recommended)')
        )

        if gemini_config is not None:
            old_model = gemini_config.get('model_name', '')

            # v8: pre-v8 users didn't have a custom-model field, so we can
            # bump unconditionally. We skip the historical "v8 default of
            # gemma-3-27b-it" intermediate stop and jump straight to the
            # current default.
            if needs_v8:
                if old_model != NEW_DEFAULT_MODEL:
                    gemini_config['model_name'] = NEW_DEFAULT_MODEL
                    logging.info(f'[v8] Bumped Gemini model "{old_model}" -> "{NEW_DEFAULT_MODEL}"')
                    config_changed = True

                # Obfuscate the API key. The helper is idempotent — already-
                # obfuscated keys (with the `enc:` prefix) pass through
                # unchanged.
                api_key = gemini_config.get('api_key', '')
                if api_key:
                    new_key = obfuscate_api_key(api_key)
                    if new_key != api_key:
                        gemini_config['api_key'] = new_key
                        logging.info('[v8] Obfuscated plaintext Gemini API key')
                        config_changed = True

            # v9: only runs for users coming from v8. Preserve tier choice via
            # the V8_TO_V9_MAP so an unlimited-tier user doesn't get silently
            # downgraded to a daily-quota model. Custom values (anything not
            # in the map) are left alone.
            elif needs_v9:
                new_model = V8_TO_V9_MAP.get(old_model)
                if new_model is not None and new_model != old_model:
                    gemini_config['model_name'] = new_model
                    logging.info(f'[v9] Bumped Gemini model "{old_model}" -> "{new_model}"')
                    config_changed = True

        # Stamp every version flag up to current so we never re-run on
        # subsequent startups, even if no fields actually needed changing
        # (e.g., a v8 user who'd already picked a custom non-deprecated model).
        for n in range(8, CURRENT_CONFIG_VERSION + 1):
            self.config[f'is_config_file_updated_for_v{n}'] = True

        self.save_config(self.config)
        logging.info('Config migration complete')

        # Single restart popup, regardless of how many versions we jumped.
        if config_changed:
            # Use QMessageBox directly — signals aren't connected yet at this
            # point in __init__.
            QMessageBox.information(
                None,
                'Writing Tools Updated',
                'Writing Tools has just completed an internal update '
                '(your config.json was migrated to the current format).\n\n'
                'Please restart Writing Tools.'
            )
            sys.exit(0)

    def load_options(self):
        """
        Load the options file.
        """
        self.options_path = get_resource_path('options.json')
        logging.debug(f'Loading options from {self.options_path}')
        if os.path.exists(self.options_path):
            with open(self.options_path, 'r', encoding='utf-8') as f:
                self.options = json.load(f)
                logging.debug('Options loaded successfully')
        else:
            logging.debug('Options file not found')
            self.options = None

    def save_config(self, config):
        """
        Save the configuration file.
        """
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=4)
            logging.debug('Config saved successfully')
        self.config = config

    def save_window_size(self, window_name, width, height):
        """
        Save window dimensions to config.json.
        """
        if not hasattr(self, 'config') or self.config is None:
            self.config = {}
        if 'window_sizes' not in self.config:
            self.config['window_sizes'] = {}
        if self.config['window_sizes'].get(window_name) != [width, height]:
            self.config['window_sizes'][window_name] = [width, height]
            self.save_config(self.config)

    def save_window_geometry(self, window_name, x, y, width, height):
        """
        Save window location (x, y) and dimensions (width, height) to config.json.
        """
        if not hasattr(self, 'config') or self.config is None:
            self.config = {}
        if 'window_geometries' not in self.config:
            self.config['window_geometries'] = {}
        if 'window_sizes' not in self.config:
            self.config['window_sizes'] = {}

        self.config['window_sizes'][window_name] = [width, height]
        if self.config['window_geometries'].get(window_name) != [x, y, width, height]:
            self.config['window_geometries'][window_name] = [x, y, width, height]
            self.save_config(self.config)

    def apply_window_geometry(self, window, window_name, fallback_width=540, fallback_height=640, near_cursor=False):
        """
        Applies saved window geometry (position and size), clamping to the active screen's
        available work area (excluding taskbar) so the window fits comfortably on laptops or small screens.
        """
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()

        saved_geom = self.config.get('window_geometries', {}).get(window_name) if self.config else None
        saved_size = self.config.get('window_sizes', {}).get(window_name) if self.config else None

        if saved_geom and len(saved_geom) == 4:
            x, y, w, h = saved_geom
            # Check which screen contains the saved point
            saved_screen = QGuiApplication.screenAt(QtCore.QPoint(x, y))
            if saved_screen is not None:
                screen = saved_screen
            avail = screen.availableGeometry()
            w = max(window.minimumWidth(), min(w, avail.width()))
            h = max(window.minimumHeight(), min(h, avail.height()))
            # Clamp position within available work area
            x = max(avail.left(), min(x, avail.right() - w))
            y = max(avail.top(), min(y, avail.bottom() - h))
        else:
            avail = screen.availableGeometry()
            if saved_size and len(saved_size) == 2:
                w, h = saved_size
            else:
                w, h = fallback_width, fallback_height
            w = max(window.minimumWidth(), min(w, avail.width()))
            h = max(window.minimumHeight(), min(h, avail.height()))

            if near_cursor:
                x = cursor_pos.x()
                y = cursor_pos.y() + 20
                if x + w > avail.right():
                    x = avail.right() - w
                if y + h > avail.bottom():
                    y = cursor_pos.y() - h - 10
                x = max(avail.left(), min(x, avail.right() - w))
                y = max(avail.top(), min(y, avail.bottom() - h))
            else:
                x = avail.left() + (avail.width() - w) // 2
                y = avail.top() + (avail.height() - h) // 2

        window.resize(w, h)
        window.move(x, y)

    def get_multi_variation_instruction(self, option, system_instruction):
        """
        Builds a multi-variation prompt tailored specifically to the given option/command.
        """
        option_lower = option.lower().strip()
        if option_lower in ('proofread', 'rewrite'):
            style_guidance = (
                '    {"label": "Option 1: Direct Grammar & Spelling Fix", "text": "First variation..."},\n'
                '    {"label": "Option 2: Smooth & Natural Rewording", "text": "Second variation..."},\n'
                '    {"label": "Option 3: Polished & Refined Phrasing", "text": "Third variation..."}\n'
            )
            goal_desc = "Provide exactly THREE (3) distinct proofreading/rewording variations for the user's text, focusing on fixing grammar, punctuation, spelling, and sentence phrasing while preserving the core meaning."
        elif option_lower == 'friendly':
            style_guidance = (
                '    {"label": "Option 1: Warm & Casual", "text": "First variation..."},\n'
                '    {"label": "Option 2: Lighthearted & Friendly", "text": "Second variation..."},\n'
                '    {"label": "Option 3: Conversational & Approachable", "text": "Third variation..."}\n'
            )
            goal_desc = "Provide exactly THREE (3) distinct friendly variations for the user's text with different nuances of warmth, casualness, and friendliness."
        elif option_lower == 'professional':
            style_guidance = (
                '    {"label": "Option 1: Formal & Executive", "text": "First variation..."},\n'
                '    {"label": "Option 2: Clear Corporate", "text": "Second variation..."},\n'
                '    {"label": "Option 3: Polished & Business Professional", "text": "Third variation..."}\n'
            )
            goal_desc = "Provide exactly THREE (3) distinct professional variations for the user's text, maintaining a formal, clear, and business-appropriate tone."
        elif option_lower == 'concise':
            style_guidance = (
                '    {"label": "Option 1: Ultra-Concise & Direct", "text": "First variation..."},\n'
                '    {"label": "Option 2: Tight & Essential", "text": "Second variation..."},\n'
                '    {"label": "Option 3: Balanced Shortened", "text": "Third variation..."}\n'
            )
            goal_desc = "Provide exactly THREE (3) distinct concise variations for the user's text, trimming wordiness while preserving key points."
        else:
            style_guidance = (
                '    {"label": "Option 1: Direct & Clear", "text": "First variation..."},\n'
                '    {"label": "Option 2: Polished & Well-Structured", "text": "Second variation..."},\n'
                '    {"label": "Option 3: Closest to Original Intent", "text": "Third variation..."}\n'
            )
            goal_desc = f"Provide exactly THREE (3) distinct writing variations for the user's text based on the requested command ({option})."

        return (
            f"{system_instruction}\n\n"
            f"CRITICAL REQUIREMENT: {goal_desc}\n"
            "Format your response ONLY as a JSON object with the structure:\n"
            "{\n"
            '  "variations": [\n'
            f"{style_guidance}"
            "  ]\n"
            "}\n"
            "Do not include any additional chat or commentary outside the JSON."
        )

    def show_onboarding(self):
        """
        Show the onboarding window for first-time users.
        """
        logging.debug('Showing onboarding window')
        self.onboarding_window = ui.OnboardingWindow.OnboardingWindow(self)
        self.onboarding_window.close_signal.connect(self.exit_app)
        self.onboarding_window.show()

    @staticmethod
    def _to_pynput_hotkey(hotkey_str):
        """
        Convert a user-facing hotkey string ('ctrl+j', 'ctrl+alt+space') to
        pynput's `<ctrl>+j` / `<ctrl>+<alt>+<space>` format. Single-char keys
        stay as-is; multi-char keys (modifiers, named keys) get wrapped in <>.
        """
        return '+'.join(
            f'{t}' if len(t) <= 1 else f'<{t}>'
            for t in hotkey_str.split('+')
        )

    def start_hotkey_listener(self):
        """
        Build a single `GlobalHotKeys` listener that handles both the main
        Writing Tools shortcut AND any per-button direct hotkeys defined in
        options.json. Per-button hotkeys fire the corresponding option
        immediately, skipping the popup.

        On conflict between a button hotkey and the global shortcut (or
        between two button hotkeys), the first registration wins and the
        later one is logged and skipped — the listener can't dispatch to
        two callbacks for the same combination anyway.
        """
        try:
            if self.hotkey_listener is not None:
                self.hotkey_listener.stop()
                self.hotkey_listener = None

            hotkey_map = {}

            # --- Global Writing Tools hotkey ----------------------------------
            orig_shortcut = self.config.get('shortcut', 'ctrl+space')
            self.registered_hotkey = orig_shortcut
            try:
                global_parsed = self._to_pynput_hotkey(orig_shortcut)
                # Validate by parsing — raises if malformed.
                pykeyboard.HotKey.parse(global_parsed)

                def on_global_activate():
                    if self.paused:
                        return
                    self._capture_active_window()
                    logging.debug('triggered global hotkey')
                    self.hotkey_triggered_signal.emit()

                hotkey_map[global_parsed] = on_global_activate
                logging.debug(f'Registered global hotkey: {global_parsed}')
            except Exception as e:
                logging.error(f'Failed to parse global hotkey "{orig_shortcut}": {e}')

            # --- Per-button direct hotkeys ------------------------------------
            # Custom is excluded — it needs a typed instruction from the user,
            # so a "fire directly" hotkey doesn't make sense for it.
            if self.options:
                for button_name, button_cfg in self.options.items():
                    if button_name == 'Custom':
                        continue
                    raw = (button_cfg.get('hotkey') or '').strip()
                    if not raw:
                        continue
                    try:
                        parsed = self._to_pynput_hotkey(raw)
                        # Same validation path as the global shortcut.
                        pykeyboard.HotKey.parse(parsed)
                    except Exception as e:
                        logging.error(
                            f'Invalid hotkey "{raw}" for button "{button_name}": {e}'
                        )
                        continue
                    if parsed in hotkey_map:
                        logging.warning(
                            f'Hotkey "{raw}" for button "{button_name}" '
                            f'conflicts with an already-registered binding; skipping'
                        )
                        continue
                    hotkey_map[parsed] = self._make_button_hotkey_callback(button_name)
                    logging.debug(f'Registered button hotkey: {parsed} -> {button_name}')

            if not hotkey_map:
                logging.warning('No hotkeys to register')
                return

            self.hotkey_listener = pykeyboard.GlobalHotKeys(hotkey_map)
            self.hotkey_listener.start()
        except Exception as e:
            logging.error(f'Failed to register hotkey listener: {e}')

    def _capture_active_window(self):
        """Capture the currently active/foreground window so we can restore focus later."""
        if sys.platform.startswith('win32'):
            try:
                import ctypes
                self.last_active_window = ctypes.windll.user32.GetForegroundWindow()
                logging.debug(f'Captured active window HWND: {self.last_active_window}')
            except Exception as e:
                logging.error(f'Error capturing active window HWND: {e}')
                self.last_active_window = None
        else:
            self.last_active_window = None

    def _restore_active_window(self):
        """Restore focus to the previously active window before simulating paste."""
        if sys.platform.startswith('win32') and self.last_active_window:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hwnd = self.last_active_window
                if user32.IsWindow(hwnd):
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.08)  # Allow Windows to transition focus
            except Exception as e:
                logging.error(f'Error restoring active window: {e}')

    def _make_button_hotkey_callback(self, button_name):
        """
        Build a callback that fires `button_name` directly, bypassing the
        popup. Closes over `button_name` so we can register many distinct
        callbacks in a single GlobalHotKeys map.

        The actual fire is bounced through the Qt event loop because pynput
        invokes callbacks on its listener thread; popup/clipboard work needs
        to happen on the main thread.
        """
        def callback():
            if self.paused:
                return
            self._capture_active_window()
            logging.debug(f'Direct hotkey fired for button "{button_name}"')
            # Match the global hotkey's behaviour: cancel any in-flight
            # request so a new fire doesn't pile up on top of a previous one.
            if self.current_provider:
                self.current_provider.cancel()
                self.output_queue = ""
            # noinspection PyTypeChecker
            QtCore.QMetaObject.invokeMethod(
                self,
                "_fire_button_directly",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, button_name)
            )
        return callback

    @Slot(str)
    def _fire_button_directly(self, button_name):
        """
        Run a button's option without showing the popup — invoked by a
        per-button direct hotkey. Mirrors the relevant parts of
        `_show_popup`: set up the async clipboard capture, then hand off
        to the worker thread (`process_option`) which waits on the holder
        and dispatches the AI call.
        """
        logging.debug(f'Firing button "{button_name}" directly')

        # If the popup is currently visible (e.g., user opened it then
        # pressed a button hotkey), close it so it doesn't compete.
        if self.popup_window is not None and self.popup_window.isVisible():
            self.popup_window.close()

        # Sanity check: button must still exist in options. Could be stale
        # if options.json was edited externally between registration and
        # fire — skip gracefully rather than crash.
        if not self.options or button_name not in self.options:
            logging.warning(f'Button "{button_name}" no longer exists; ignoring hotkey')
            return

        self.current_text_holder = _SelectedTextHolder()
        self._fire_ctrl_c_and_capture_async(self.current_text_holder)

        # Same worker path as a popup-button click. process_option_thread
        # waits on the holder, surfaces "Please select text…" if empty,
        # and routes window-mode options through the response window.
        self.process_option(button_name)

    def register_hotkey(self):
        """
        Register the global hotkey for activating Writing Tools.
        """
        logging.debug('Registering hotkey')
        self.start_hotkey_listener()
        logging.debug('Hotkey registered')

    def on_hotkey_pressed(self):
        """
        Handle the hotkey press event.
        """
        self._capture_active_window()
        logging.debug('Hotkey pressed')
        
        # Check for spam triggers
        if self.check_trigger_spam():
            logging.warning('Hotkey spam detected - quitting application')
            self.exit_app()
            return
            
        # Original hotkey handling continues...
        if self.current_provider:
            logging.debug("Cancelling current provider's request")
            self.current_provider.cancel()
            self.output_queue = ""

        # noinspection PyTypeChecker
        QtCore.QMetaObject.invokeMethod(self, "_show_popup", QtCore.Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _show_popup(self):
        """
        Initiate selected text capture and schedule popup display.
        We delay showing the popup by ~75ms so the target application (Chrome,
        Edge, Word, etc.) has adequate time to receive and process simulated Ctrl+C
        while still being the active foreground window.
        """
        logging.debug('Showing popup window')

        # Fresh holder per popup. Fire Ctrl+C *before* we create the popup
        # so the keystroke is queued while focus is still on the user's source app.
        self.current_text_holder = _SelectedTextHolder()
        self._fire_ctrl_c_and_capture_async(self.current_text_holder)

        # Brief delay before showing/activating popup to prevent stealing foreground focus prematurely
        QtCore.QTimer.singleShot(75, self._display_popup_window)

    def _display_popup_window(self):
        """
        Creates and positions the CustomPopupWindow after the target app
        has had time to process the copy keystroke.
        """
        try:
            if self.popup_window is not None:
                logging.debug('Existing popup window found')
                if self.popup_window.isVisible():
                    logging.debug('Closing existing visible popup window')
                    self.popup_window.close()
                self.popup_window = None
            logging.debug('Creating new popup window')
            self.popup_window = ui.CustomPopupWindow.CustomPopupWindow(self)

            # Set the window icon
            icon_path = os.path.join(os.path.dirname(sys.argv[0]), 'icons', 'app_icon.png')
            if os.path.exists(icon_path): self.setWindowIcon(QtGui.QIcon(icon_path))
            # Get the screen containing the cursor
            cursor_pos = QCursor.pos()
            screen = QGuiApplication.screenAt(cursor_pos)
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            # Use availableGeometry to avoid overlapping the taskbar
            screen_geometry = screen.availableGeometry()
            logging.debug(f'Cursor is on screen: {screen.name()}')
            logging.debug(f'Screen available geometry: {screen_geometry}')
            # Show the popup to get its size
            self.popup_window.show()
            self.popup_window.adjustSize()
            # Ensure the popup it's focused, even on lower-end machines
            self.popup_window.activateWindow()
            QtCore.QTimer.singleShot(100, self.popup_window.custom_input.setFocus)

            popup_width = self.popup_window.width()
            popup_height = self.popup_window.height()
            # Calculate position
            x = cursor_pos.x()
            y = cursor_pos.y() + 20  # 20 pixels below cursor
            # Adjust if the popup would go off the right edge of the work area
            if x + popup_width > screen_geometry.right():
                x = screen_geometry.right() - popup_width
            # Adjust if the popup would go off the bottom edge of the work area
            if y + popup_height > screen_geometry.bottom():
                y = cursor_pos.y() - popup_height - 10  # 10 pixels above cursor
            x = max(screen_geometry.left(), min(x, screen_geometry.right() - popup_width))
            y = max(screen_geometry.top(), min(y, screen_geometry.bottom() - popup_height))
            self.popup_window.move(x, y)
            logging.debug(f'Popup window moved to position: ({x}, {y})')
        except Exception as e:
            logging.error(f'Error showing popup window: {e}', exc_info=True)

    def _fire_ctrl_c_and_capture_async(self, holder):
        """
        Inject Ctrl+C now (must happen while focus is still on the user's
        source app, before the popup is shown), then poll the clipboard
        for the result in a background thread. Returns immediately so the
        popup can display with minimal delay.

        Slow systems' clipboard subsystems can take a while to populate
        after Ctrl+C — that's the whole reason this is async. The polling
        timeout is generous; an empty result after timeout means the user
        pressed the hotkey without actually selecting anything, which
        `process_option_thread` reports as a normal error.
        """
        try:
            clipboard_backup = pyperclip.paste()
        except Exception:
            clipboard_backup = ''

        self.clear_clipboard()

        kbrd = pykeyboard.Controller()
        try:
            # Release trigger/modifier keys that might still be physically held down
            # (e.g. Space when using Ctrl+Space, or Alt/Shift) so the target app
            # registers a pure Ctrl+C instead of Ctrl+Space+C.
            for mod_key in (pykeyboard.Key.space, pykeyboard.Key.alt, pykeyboard.Key.shift):
                try:
                    kbrd.release(mod_key)
                except Exception:
                    pass

            time.sleep(0.015)
            kbrd.press(pykeyboard.Key.ctrl)
            time.sleep(0.015)
            kbrd.press('c')
            time.sleep(0.015)
            kbrd.release('c')
            time.sleep(0.015)
            kbrd.release(pykeyboard.Key.ctrl)
        except Exception as e:
            logging.error(f'Error simulating Ctrl+C: {e}')

        def _poll_clipboard():
            # Lock so concurrent hotkey presses don't trample each other's
            # in-flight captures.
            with self._capture_lock:
                text = ''
                try:
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        try:
                            text = pyperclip.paste() or ''
                        except Exception as e:
                            logging.error(f'Error reading clipboard during poll: {e}')
                            text = ''
                        if text:
                            break
                        time.sleep(0.05)
                    holder.text = text
                    logging.debug(f'Captured selected text (len={len(text)})')
                finally:
                    try:
                        pyperclip.copy(clipboard_backup)
                    except Exception as e:
                        logging.error(f'Error restoring clipboard: {e}')
                    holder.ready.set()

        threading.Thread(target=_poll_clipboard, daemon=True).start()

    @staticmethod
    def clear_clipboard():
        """
        Clear the system clipboard.
        """
        try:
            pyperclip.copy('')
        except Exception as e:
            logging.error(f'Error clearing clipboard: {e}')

    def process_option(self, option, custom_change=None):
        """
        Spawn a worker thread that waits for the asynchronous clipboard
        capture and then runs the chosen option. Kept as a thin wrapper so
        the popup's click handler returns immediately and the GUI thread
        is never blocked on the clipboard read.
        """
        logging.debug(f'Processing option: {option}')

        # Drop any stale ref so a previous run's late-arriving response can't
        # land in a now-irrelevant window. The new window (if any) is created
        # by the worker via `_setup_response_window` once the text is in.
        if hasattr(self, 'current_response_window'):
            delattr(self, 'current_response_window')

        threading.Thread(
            target=self.process_option_thread,
            args=(option, custom_change),
            daemon=True
        ).start()

    @Slot(str, str)
    def _setup_response_window(self, option, selected_text):
        """
        Open the response window and seed its chat history. Called from
        `process_option_thread` via `BlockingQueuedConnection` so the
        worker can rely on the window existing before it dispatches the
        AI request.
        """
        self.current_response_window = self.show_response_window(option, selected_text)
        self.current_response_window.chat_history = [
            {
                "role": "user",
                "content": f"Original text to {option.lower()}:\n\n{selected_text}"
            }
        ]

    def process_option_thread(self, option, custom_change=None):
        """
        Worker: wait for the background clipboard capture to land, then
        either open a response window (for window-mode options) or set up
        for inline replacement, and finally run the AI request.
        """
        logging.debug(f'Starting processing thread for option: {option}')

        # Typically near-instant since the user took time to read the popup
        # and click. The 3s ceiling is a safety net for genuinely sluggish
        # systems; if the 2s polling deadline in the capture thread tripped
        # first, the event is already set and this returns immediately.
        holder = self.current_text_holder
        if holder is None or not holder.ready.wait(timeout=3.0):
            logging.warning('Timed out waiting for selected text capture')
        selected_text = (holder.text if holder else '') or ''

        if not selected_text.strip():
            # The chat-mode fallback that used to fire here was removed when
            # popup show became instant — we no longer have a way to detect
            # "user wants to chat" vs "capture failed", so we pick the safer
            # interpretation and surface the error.
            self.show_message_signal.emit('Error', 'Please select text to use this option.')
            return

        if self.options[option]['open_in_window']:
            QtCore.QMetaObject.invokeMethod(
                self,
                '_setup_response_window',
                QtCore.Qt.ConnectionType.BlockingQueuedConnection,
                QtCore.Q_ARG(str, option),
                QtCore.Q_ARG(str, selected_text)
            )

        try:
            selected_prompt = self.options.get(option, ('', ''))
            prompt_prefix = selected_prompt['prefix']
            system_instruction = selected_prompt['instruction']
            if option == 'Custom':
                prompt = f"{prompt_prefix}Described change: {custom_change}\n\nText: {selected_text}"
            else:
                prompt = f"{prompt_prefix}{selected_text}"

            self.output_queue = ""

            logging.debug(f'Getting response from provider for option: {option}')

            if self.options[option]['open_in_window']:
                logging.debug('Getting response for window display')
                response = self.current_provider.get_response(system_instruction, prompt, return_response=True)
                logging.debug(f'Got response of length: {len(response) if response else 0}')

                if hasattr(self, 'current_response_window'):
                    # noinspection PyTypeChecker
                    QtCore.QMetaObject.invokeMethod(
                        self.current_response_window,
                        'set_text',
                        QtCore.Qt.ConnectionType.QueuedConnection,
                        QtCore.Q_ARG(str, response)
                    )
                    logging.debug('Invoked set_text on response window')
            else:
                logging.debug('Getting 3 variations for preview window selection')
                multi_variation_instruction = self.get_multi_variation_instruction(option, system_instruction)

                # Show the window immediately with placeholder loading cards so
                # the user sees instant feedback, then fill in results progressively.
                self.show_variation_loading_signal.emit(selected_text)

                last_count = [0]
                def _stream_chunk_callback(partial_text):
                    partial_vars = parse_variations_response(partial_text)
                    if len(partial_vars) > last_count[0]:
                        last_count[0] = len(partial_vars)
                        logging.debug(f'Streamed {len(partial_vars)} variation(s) so far')
                        self.update_preview_variations_signal.emit(partial_vars)

                response = self.current_provider.get_response_stream(
                    multi_variation_instruction, prompt, on_chunk=_stream_chunk_callback
                )
                if not response:
                    logging.debug('Empty response received (request may have been cancelled).')
                    return

                variations = parse_variations_response(response)
                logging.debug(f'Parsed {len(variations)} final variations for option {option}')

                self.update_preview_variations_signal.emit(variations)

        except Exception as e:
            if hasattr(self, 'current_provider') and getattr(self.current_provider, 'close_requested', False):
                logging.debug('Request cancelled cleanly by user dismissal.')
                return

            logging.error(f'An error occurred: {e}', exc_info=True)

            if "Resource has been exhausted" in str(e):
                self.show_message_signal.emit('Error - Rate Limit Hit', 'Whoops! You\'ve hit the per-minute rate limit of the Gemini API. Please try again in a few moments.\n\nIf this happens often, simply switch to a Gemini model with a higher usage limit in Settings.')
            else:
                self.show_message_signal.emit('Error', f'An error occurred: {e}')

    @Slot(str)
    def _show_variation_loading_window(self, selected_text):
        """
        Opens the VariationPreviewWindow immediately with a loading state.
        The background thread will call update_preview_variations_signal progressively
        as variations are streamed, which populates the real cards via update_variations().
        """
        try:
            # Open with empty variations so the window enters loading state.
            preview_window = ui.VariationPreviewWindow.VariationPreviewWindow(
                parent=None,
                original_text=selected_text,
                variations=[],   # triggers loading placeholder cards
                app=self
            )
            self.current_preview_window = preview_window
            preview_window.refinement_requested.connect(self._handle_refinement_request)
            self.update_preview_variations_signal.connect(preview_window.update_variations)

            preview_window.show()
            self.apply_window_geometry(preview_window, 'VariationPreviewWindow', fallback_width=540, fallback_height=640, near_cursor=True)
            preview_window.activateWindow()

            # Block until user picks a variation or cancels
            if preview_window.exec_() == QtWidgets.QDialog.DialogCode.Accepted and preview_window.selected_variation:
                logging.debug('User selected a variation from loading preview window')
                self.paste_selected_variation(preview_window.selected_variation)
            else:
                logging.debug('User cancelled variation loading preview window')

        except Exception as e:
            logging.error(f'Error displaying variation loading window: {e}', exc_info=True)
        finally:
            self.current_preview_window = None

    @Slot(str, list)
    def _show_variation_preview_window(self, selected_text, variations):
        """
        Shows the VariationPreviewWindow on the main UI thread (legacy path with pre-loaded variations).
        """
        try:
            preview_window = ui.VariationPreviewWindow.VariationPreviewWindow(
                parent=None,
                original_text=selected_text,
                variations=variations,
                app=self
            )
            self.current_preview_window = preview_window
            preview_window.refinement_requested.connect(self._handle_refinement_request)

            preview_window.show()
            self.apply_window_geometry(preview_window, 'VariationPreviewWindow', fallback_width=540, fallback_height=640, near_cursor=True)
            preview_window.activateWindow()

            if preview_window.exec_() == QtWidgets.QDialog.DialogCode.Accepted and preview_window.selected_variation:
                logging.debug('User selected a variation from preview window')
                self.paste_selected_variation(preview_window.selected_variation)
            else:
                logging.debug('User cancelled variation preview window')

        except Exception as e:
            logging.error(f'Error displaying variation preview window: {e}', exc_info=True)
        finally:
            self.current_preview_window = None

    @Slot(str, str)
    def _handle_refinement_request(self, original_text, instruction):
        """
        Handles async refinement request from the VariationPreviewWindow with progressive streaming.
        """
        logging.debug(f'Processing refinement request: {instruction}')

        def _refinement_worker():
            try:
                multi_variation_instruction = (
                    "You are a writing assistant. The user wants to refine previous writing variations based on a specific request.\n"
                    "Provide exactly THREE (3) distinct new writing variations/rewrites for the user's text following the refinement instruction.\n"
                    "Format your output ONLY as a JSON object with the structure:\n"
                    "{\n"
                    '  "variations": [\n'
                    '    {"label": "Option 1: Refined Variation 1", "text": "First variation..."},\n'
                    '    {"label": "Option 2: Refined Variation 2", "text": "Second variation..."},\n'
                    '    {"label": "Option 3: Refined Variation 3", "text": "Third variation..."}\n'
                    "  ]\n"
                    "}\n"
                    "Do not include any extra chat or commentary outside the JSON."
                )
                prompt = f"Original Text:\n{original_text}\n\nRefinement Instruction: {instruction}"

                last_count = [0]
                def _refine_stream_callback(partial_text):
                    partial_vars = parse_variations_response(partial_text)
                    if len(partial_vars) > last_count[0]:
                        last_count[0] = len(partial_vars)
                        self.update_preview_variations_signal.emit(partial_vars)

                response = self.current_provider.get_response_stream(
                    multi_variation_instruction, prompt, on_chunk=_refine_stream_callback
                )
                variations = parse_variations_response(response)
                logging.debug(f'Parsed {len(variations)} refined variations')

                self.update_preview_variations_signal.emit(variations)
            except Exception as e:
                logging.error(f'Error processing refinement: {e}', exc_info=True)
                self.show_message_signal.emit('Error', f'Failed to regenerate variations: {e}')
                if self.current_preview_window and self.current_preview_window.isVisible():
                    QtCore.QMetaObject.invokeMethod(
                        self.current_preview_window,
                        'set_loading_state',
                        QtCore.Qt.ConnectionType.QueuedConnection,
                        QtCore.Q_ARG(bool, False)
                    )

        threading.Thread(target=_refinement_worker, daemon=True).start()

    @Slot(list)
    def _update_current_preview_variations(self, variations):
        """
        Slot to update the active preview window with newly generated variations.
        """
        if self.current_preview_window and self.current_preview_window.isVisible():
            self.current_preview_window.update_variations(variations)

    def paste_selected_variation(self, text):
        """
        Copies selected variation to clipboard, restores focus to the active target application,
        simulates Ctrl+V paste into active application, and restores previous clipboard content.
        """
        try:
            clipboard_backup = pyperclip.paste()
        except Exception:
            clipboard_backup = ''

        try:
            self._restore_active_window()

            cleaned_text = text.rstrip('\n')
            pyperclip.copy(cleaned_text)

            kbrd = pykeyboard.Controller()
            time.sleep(0.02)
            kbrd.press(pykeyboard.Key.ctrl)
            time.sleep(0.02)
            kbrd.press('v')
            time.sleep(0.02)
            kbrd.release('v')
            time.sleep(0.02)
            kbrd.release(pykeyboard.Key.ctrl)

            time.sleep(0.5)
        except Exception as e:
            logging.error(f'Error pasting selected variation: {e}')
        finally:
            try:
                pyperclip.copy(clipboard_backup)
            except Exception as e:
                logging.error(f'Error restoring clipboard after variation paste: {e}')

    @Slot(str, str)
    def show_message_box(self, title, message):
        """
        Show a message box with the given title and message.
        """
        QMessageBox.warning(None, title, message)

    def show_response_window(self, option, text):
        """
        Show the response in a new window instead of pasting it.
        """
        response_window = ui.ResponseWindow.ResponseWindow(self, f"{option} Result")
        response_window.selected_text = text  # Store the text for regeneration
        response_window.show()
        self.apply_window_geometry(response_window, 'ResponseWindow', fallback_width=600, fallback_height=450, near_cursor=False)
        response_window._size_initialized = True
        return response_window

    def replace_text(self, new_text):
        """
        Replaces the text by pasting in the LLM generated text. With "Key Points" and "Summary", invokes a window with the output instead.
        """
        error_message = 'ERROR_TEXT_INCOMPATIBLE_WITH_REQUEST'

        # Confirm new_text exists and is a string
        if new_text and isinstance(new_text, str):
            self.output_queue += new_text
            current_output = self.output_queue.strip()  # Strip whitespace for comparison

            # If the new text is the error message, show a message box
            if current_output == error_message:
                self.show_message_signal.emit('Error', 'The text is incompatible with the requested change.')
                return

            # Check if we're building up to the error message (to prevent partial pasting)
            if len(current_output) <= len(error_message):
                clean_current = ''.join(current_output.split())
                clean_error = ''.join(error_message.split())
                if clean_current == clean_error[:len(clean_current)]:
                    return

            logging.debug('Processing output text')
            try:
                # For Summary and Key Points, show in response window
                if hasattr(self, 'current_response_window'):
                    self.current_response_window.append_text(new_text)
                    
                    # If this is the initial response, add it to chat history
                    if len(self.current_response_window.chat_history) == 1:  # Only original text exists
                        self.current_response_window.chat_history.append({
                            "role": "assistant",
                            "content": self.output_queue.rstrip('\n')
                        })
                else:
                    # For other options, use the original clipboard-based replacement
                    self._restore_active_window()

                    clipboard_backup = pyperclip.paste()
                    cleaned_text = self.output_queue.rstrip('\n')
                    pyperclip.copy(cleaned_text)
                    
                    kbrd = pykeyboard.Controller()
                    def press_ctrl_v():
                        time.sleep(0.02)
                        kbrd.press(pykeyboard.Key.ctrl)
                        time.sleep(0.02)
                        kbrd.press('v')
                        time.sleep(0.02)
                        kbrd.release('v')
                        time.sleep(0.02)
                        kbrd.release(pykeyboard.Key.ctrl)

                    press_ctrl_v()
                    time.sleep(0.5)
                    pyperclip.copy(clipboard_backup)

                if not hasattr(self, 'current_response_window'):
                    self.output_queue = ""

            except Exception as e:
                logging.error(f'Error processing output: {e}')
        else:
            logging.debug('No new text to process')

    def create_tray_icon(self):
        """
        Create the system tray icon for the application.
        """
        if self.tray_icon:
            logging.debug('Tray icon already exists')
            return

        logging.debug('Creating system tray icon')
        icon_path = get_resource_path(os.path.join('icons', 'app_icon.png'))
        if not os.path.exists(icon_path):
            icon_path = get_resource_path(os.path.join('icons', 'app_icon.ico'))

        if os.path.exists(icon_path):
            self.tray_icon = QtWidgets.QSystemTrayIcon(QtGui.QIcon(icon_path), self)
        else:
            logging.warning(f'Tray icon not found at {icon_path}')
            self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        # Set the tooltip (hover name) for the tray icon
        self.tray_icon.setToolTip("WritingTools")
        self.tray_menu = QtWidgets.QMenu()
        self.tray_icon.setContextMenu(self.tray_menu)

        self.update_tray_menu()
        self.tray_icon.show()
        logging.debug('Tray icon displayed')

    def update_tray_menu(self):
        """
        Update the tray menu with all menu items, including pause functionality
        and proper translations.
        """
        self.tray_menu.clear()

        # Apply dark mode styles using darkdetect
        self.apply_dark_mode_styles(self.tray_menu)

        # Settings menu item
        settings_action = self.tray_menu.addAction(self._('Settings'))
        settings_action.triggered.connect(self.show_settings)

        # Pause/Resume toggle action 
        self.toggle_action = self.tray_menu.addAction(self._('Resume') if self.paused else self._('Pause'))
        self.toggle_action.triggered.connect(self.toggle_paused)

        # About menu item
        about_action = self.tray_menu.addAction(self._('About'))
        about_action.triggered.connect(self.show_about)

        # Exit menu item
        exit_action = self.tray_menu.addAction(self._('Exit'))
        exit_action.triggered.connect(self.exit_app)
        
    def toggle_paused(self):
        """Toggle the paused state of the application."""
        logging.debug('Toggle paused state')
        self.paused = not self.paused
        self.toggle_action.setText(self._('Resume') if self.paused else self._('Pause'))
        logging.debug('App is paused' if self.paused else 'App is resumed')

    @staticmethod
    def apply_dark_mode_styles(menu):
        """
        Apply styles to the tray menu based on system theme using darkdetect.
        """
        is_dark_mode = darkdetect.isDark()
        palette = menu.palette()

        if is_dark_mode:
            logging.debug('Tray icon dark')
            # Dark mode colors
            palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#2d2d2d"))  # Dark background
            palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#ffffff"))  # White text
        else:
            logging.debug('Tray icon light')
            # Light mode colors
            palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#ffffff"))  # Light background
            palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#000000"))  # Black text

        menu.setPalette(palette)


    """
    The function below (process_followup_question) processes follow-up questions in the chat interface for Summary, Key Points, and Table operations.

    This method handles the complex interaction between the UI, chat history, and AI providers:

    1. Chat History Management:
    - Maintains a list of all messages (original text, summary, follow-ups)
    - Properly formats roles (user/assistant) for each message
    - Preserves conversation context across multiple questions (until the Window is closed)

    2. Provider-Specific Handling:
    a) Gemini:
        - Converts internal roles to Gemini's user/model format
        - Uses chat sessions with proper history formatting
        - Maintains context through chat.send_message()
    
    b) OpenAI-compatible:
        - Uses standard OpenAI message array format
        - Includes system instruction and full conversation history
        - Properly maps internal roles to OpenAI roles

    3. Flow:
    a) User asks follow-up question
    b) Question is added to chat history
    c) Full history is formatted for the current provider
    d) Response is generated while maintaining context
    e) Response is displayed in chat UI
    f) New response is added to history for future context

    4. Threading:
    - Runs in a separate thread to prevent UI freezing
    - Uses signals to safely update UI from background thread
    - Handles errors too

    Args:
        response_window: The ResponseWindow instance managing the chat UI
        question: The follow-up question from the user

    This implementation is a bit convoluted, but it allows us to manage chat history & model roles across both providers! :3
    """

    def process_followup_question(self, response_window, question):
        """
        Process a follow-up question in the chat window.
        """
        logging.debug(f'Processing follow-up question: {question}')
        
        def process_thread():
            logging.debug('Starting follow-up processing thread')
            try:
                if not response_window.chat_history:
                    logging.error("No chat history found")
                    self.show_message_signal.emit('Error', 'Chat history not found')
                    return

                # Add current question to chat history
                response_window.chat_history.append({
                    "role": "user",
                    "content": question
                })
                
                # Get chat history
                history = response_window.chat_history.copy()
                
                # System instruction based on original option
                system_instruction = "You are a helpful AI assistant. Provide clear and direct responses, maintaining the same format and style as your previous responses. If appropriate, use Markdown formatting to make your response more readable."
                
                logging.debug('Sending request to AI provider')
                
                # Format conversation differently based on provider
                if isinstance(self.current_provider, GeminiProvider):
                    # Gemini takes the system instruction via its config object,
                    # not as an in-history message. We pass the raw chat history
                    # (user/assistant turns); GeminiProvider handles role mapping
                    # and drops any "system" entries internally.
                    response_text = self.current_provider.get_response(
                        system_instruction,
                        history,
                        return_response=True
                    )

                elif isinstance(self.current_provider, OllamaProvider):  #
                    # For Ollama, prepare messages with system instruction and history
                    messages = [{"role": "system", "content": system_instruction}]

                    for msg in history:
                        messages.append({
                            "role": msg["role"],
                            "content": msg["content"]
                        })

                    # Get response from Ollama
                    response_text = self.current_provider.get_response(
                        system_instruction,
                        messages,
                        return_response=True
                    )

                else:
                    # For OpenAI/compatible providers, prepare messages array, add system message
                    messages = [{"role": "system", "content": system_instruction}]

                    # Add history messages (including latest question)
                    for msg in history:
                        # Convert 'assistant' role to 'assistant' for OpenAI
                        role = "assistant" if msg["role"] == "assistant" else "user"
                        messages.append({"role": role, "content": msg["content"]})
                    
                    # Get response by passing the full messages array
                    response_text = self.current_provider.get_response(
                        system_instruction,
                        messages,  # Pass messages array directly
                        return_response=True
                    )

                logging.debug(f'Got response of length: {len(response_text)}')
                
                # Add response to chat history
                response_window.chat_history.append({
                    "role": "assistant",
                    "content": response_text
                })
                
                # Emit response via signal
                self.followup_response_signal.emit(response_text)

            except Exception as e:
                logging.error(f'Error processing follow-up question: {e}', exc_info=True)

                if "Resource has been exhausted" in str(e):
                    self.show_message_signal.emit('Error - Rate Limit Hit', 'Whoops! You\'ve hit the per-minute rate limit of the Gemini API. Please try again in a few moments.\n\nIf this happens often, simply switch to a Gemini model with a higher usage limit in Settings.')
                    self.followup_response_signal.emit("Sorry, an error occurred while processing your question.")
                else:
                    self.show_message_signal.emit('Error', f'An error occurred: {e}')
                    self.followup_response_signal.emit("Sorry, an error occurred while processing your question.")

        # Start the thread
        threading.Thread(target=process_thread, daemon=True).start()

    def show_settings(self, providers_only=False):

        """
        Show the settings window.
        """
        logging.debug('Showing settings window')
        # Always create a new settings window to handle providers_only correctly
        self.settings_window = ui.SettingsWindow.SettingsWindow(self, providers_only=providers_only)
        self.settings_window.close_signal.connect(self.exit_app)
        self.settings_window.retranslate_ui()
        self.settings_window.show()


    def show_about(self):
        """
        Show the about window.
        """
        logging.debug('Showing about window')
        if not self.about_window:
            self.about_window = ui.AboutWindow.AboutWindow()
        self.about_window.show()

    def setup_ctrl_c_listener(self):
        """
        Listener for Ctrl+C to exit the app.
        """
        signal.signal(signal.SIGINT, lambda signum, frame: self.handle_sigint(signum, frame))
        # This empty timer is needed to make sure that the sigint handler gets checked inside the main loop:
        # without it, the sigint handle would trigger only when an event is triggered, either by a hotkey combination
        # or by another GUI event like spawning a new window. With this we trigger it every 100ms with an empy lambda
        # so that the signal handler gets checked regularly.
        self.ctrl_c_timer = QtCore.QTimer()
        self.ctrl_c_timer.start(100)
        self.ctrl_c_timer.timeout.connect(lambda: None)
    def handle_sigint(self, signum, frame):
        """
        Handle the SIGINT signal (Ctrl+C) to exit the app gracefully.
        """
        logging.info("Received SIGINT. Exiting...")
        self.exit_app()

    def exit_app(self):
        """
        Exit the application.
        """
        logging.debug('Stopping the listener')
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        logging.debug('Exiting application')
        self.quit()
