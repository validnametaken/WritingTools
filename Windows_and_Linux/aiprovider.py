"""
AI Provider Architecture for Writing Tools
--------------------------------------------

This module handles different AI model providers (Gemini, OpenAI-compatible, Ollama) and manages their interactions
with the main application. It uses an abstract base class pattern for provider implementations.

Key Components:
1. AIProviderSetting – Base class for provider settings (e.g. API keys, model names)
    • TextSetting      – A simple text input for settings
    • DropdownSetting  – A dropdown selection setting

2. AIProvider – Abstract base class that all providers implement.
   It defines the interface for:
      • Getting a response from the AI model
      • Loading and saving configuration settings
      • Cancelling an ongoing request

3. Provider Implementations:
    • GeminiProvider – Uses Google’s Generative AI API (Gemini) to generate content.
    • OpenAICompatibleProvider – Connects to any OpenAI-compatible API (v1/chat/completions)
    • OllamaProvider – Connects to a locally running Ollama server (e.g. for llama.cpp)

Response Flow:
   • The main app calls get_response() with a system instruction and a prompt.
   • The provider formats and sends the request to its API endpoint.
   • For operations that require a window (e.g. Summary, Key Points), the provider returns the full text.
   • For direct text replacement, the provider emits the full text via the output_ready_signal.
   • Conversation history (for follow-up questions) is maintained by the main app.

Note: Streaming has been fully removed throughout the code.
"""

import base64
import json
import logging
import re
import webbrowser
from abc import ABC, abstractmethod
from typing import List, Sequence, Any

# External libraries
from google import genai
from google.genai import types as genai_types
from ollama import Client as OllamaClient
from openai import OpenAI
from PySide6 import QtWidgets
from PySide6.QtWidgets import QVBoxLayout
from ui.UIUtils import colorMode

# Obfuscation prefix to identify encrypted API keys
_OBFUSCATION_PREFIX = "enc:"
_XOR_KEY = 0x5A  # Simple XOR key for obfuscation


def obfuscate_api_key(key: str) -> str:
    """
    Obfuscate an API key using XOR + Base64 encoding.
    Returns the obfuscated string with 'enc:' prefix.
    """
    if not key or key.startswith(_OBFUSCATION_PREFIX):
        return key  # Already obfuscated or empty
    xored = bytes([b ^ _XOR_KEY for b in key.encode('utf-8')])
    return _OBFUSCATION_PREFIX + base64.b64encode(xored).decode('ascii')


def deobfuscate_api_key(obfuscated: str) -> str:
    """
    Deobfuscate an API key that was obfuscated with obfuscate_api_key().
    If the key doesn't have the 'enc:' prefix, returns it as-is (plaintext).
    """
    if not obfuscated or not obfuscated.startswith(_OBFUSCATION_PREFIX):
        return obfuscated  # Not obfuscated, return as-is
    encoded = obfuscated[len(_OBFUSCATION_PREFIX):]
    xored = base64.b64decode(encoded)
    return bytes([b ^ _XOR_KEY for b in xored]).decode('utf-8')


def parse_variations_response(raw_text: str) -> list:
    """
    Parses an LLM response requesting 3 variations into a structured list of dicts:
    [{"label": "Option 1: ...", "text": "..."}, ...]
    Supports JSON formatting, delimiter blocks (e.g. ---VARIATION 1---), and plain text fallback.
    """
    if not raw_text or not isinstance(raw_text, str):
        return [{"label": "Option 1", "text": ""}]

    cleaned = raw_text.strip()

    # 1. Try JSON parsing (direct or extracted from markdown block)
    json_str = cleaned
    if "```" in json_str:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str, re.IGNORECASE)
        if match:
            json_str = match.group(1).strip()

    start_json = min([pos for pos in (json_str.find('{'), json_str.find('[')) if pos != -1], default=-1)
    if start_json != -1:
        end_json = max(json_str.rfind('}'), json_str.rfind(']'))
        if end_json > start_json:
            json_candidate = json_str[start_json:end_json + 1]
            try:
                data = json.loads(json_candidate)
                variations = []
                if isinstance(data, dict):
                    if "variations" in data and isinstance(data["variations"], list):
                        for idx, item in enumerate(data["variations"]):
                            if isinstance(item, dict):
                                lbl = item.get("label") or f"Option {idx + 1}"
                                txt = item.get("text") or item.get("variation") or ""
                            else:
                                lbl = f"Option {idx + 1}"
                                txt = str(item)
                            if txt.strip():
                                variations.append({"label": lbl, "text": txt.strip()})
                    else:
                        default_labels = ["Option 1", "Option 2", "Option 3"]
                        for idx, (k, v) in enumerate(data.items()):
                            lbl = default_labels[idx] if idx < len(default_labels) else k
                            if isinstance(v, str) and v.strip():
                                variations.append({"label": lbl, "text": v.strip()})

                elif isinstance(data, list):
                    default_labels = ["Option 1", "Option 2", "Option 3"]
                    for idx, item in enumerate(data):
                        lbl = default_labels[idx] if idx < len(default_labels) else f"Option {idx + 1}"
                        if isinstance(item, str) and item.strip():
                            variations.append({"label": lbl, "text": item.strip()})

                if len(variations) >= 1:
                    return variations[:3]
            except Exception as e:
                logging.debug(f"JSON parsing fallback: {e}")

    # 2. Try delimiter parsing (e.g. ---VARIATION 1---, Option 1:, etc.)
    delimiter_pattern = r"(?:^|\n)[ \t]*(?:---|===|###)?[ \t]*(?:VARIATION|OPTION)[ \t]*(\d+)[: \t\-]*([^\r\n]*)"
    parts = re.split(delimiter_pattern, cleaned, flags=re.IGNORECASE)

    if len(parts) >= 4:
        variations = []
        i = 1
        while i < len(parts) - 2:
            num = parts[i]
            lbl_extra = parts[i + 1].strip(" -\t\r\n=#:")
            text = parts[i + 2].strip()
            text = re.sub(r"^(?:---|===|###)+\s*", "", text).strip()
            label = f"Option {num}" + (f": {lbl_extra}" if lbl_extra else "")
            if text:
                variations.append({"label": label, "text": text})
            i += 3
        if len(variations) >= 1:
            return variations[:3]

    # 3. Double newline split fallback if we have distinct blocks
    blocks = [b.strip() for b in cleaned.split("\n\n") if b.strip()]
    if len(blocks) == 3:
        labels = ["Option 1: Concise / Direct", "Option 2: Professional / Polished", "Option 3: Closest to Original"]
        return [{"label": labels[i], "text": blocks[i]} for i in range(3)]

    # 4. Final fallback: single response
    return [{"label": "Option 1", "text": cleaned}]



class AIProviderSetting(ABC):
    """
    Abstract base class for a provider setting (e.g., API key, model selection).
    """
    def __init__(self, name: str, display_name: str | None = None, default_value: str | None = None, description: str | None = None):
        self.name = name
        self.display_name = display_name if display_name else name
        self.default_value = default_value if default_value else ""
        self.description = description if description else ""

    @abstractmethod
    def render_to_layout(self, layout: QVBoxLayout) -> None:
        """Render the setting widget(s) into the provided layout."""
        pass

    @abstractmethod
    def set_value(self, value: str) -> None:
        """Set the internal value from configuration."""
        pass

    @abstractmethod
    def get_value(self) -> str:
        """Return the current value from the widget."""
        pass


class TextSetting(AIProviderSetting):
    """
    A text-based setting (for API keys, URLs, etc.).
    """
    def __init__(self, name: str, display_name: str | None = None, default_value: str | None = None, description: str | None = None):
        super().__init__(name, display_name, default_value, description)
        self.internal_value = default_value
        self.input = None

    def render_to_layout(self, layout: QVBoxLayout):
        row_layout = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(self.display_name)
        label.setStyleSheet(f"font-size: 16px; color: {'#ffffff' if colorMode=='dark' else '#333333'};")
        row_layout.addWidget(label)
        self.input = QtWidgets.QLineEdit(self.internal_value)
        self.input.setStyleSheet(f"""
            font-size: 16px;
            padding: 5px;
            background-color: {'#444' if colorMode=='dark' else 'white'};
            color: {'#ffffff' if colorMode=='dark' else '#000000'};
            border: 1px solid {'#666' if colorMode=='dark' else '#ccc'};
        """)
        self.input.setPlaceholderText(self.description)
        row_layout.addWidget(self.input)
        layout.addLayout(row_layout)

    def set_value(self, value):
        self.internal_value = value

    def get_value(self):
        if self.input:
            return self.input.text()
        return self.internal_value or ""


class DropdownSetting(AIProviderSetting):
    """
    A dropdown setting (e.g., for selecting a model).

    Optionally supports a "Custom" option that reveals a text input for arbitrary values.
    When allow_custom=True, users can select "Custom" from the dropdown and enter any value.
    If the loaded config value doesn't match any preset option, "Custom" is auto-selected.
    """
    # Sentinel value used internally to identify the "Custom" dropdown option
    _CUSTOM_SENTINEL = "__custom__"

    def __init__(self, name: str, display_name: str | None = None, default_value: str | None = None,
                 description: str | None = None, options: list | None = None, allow_custom: bool = False,
                 custom_placeholder: str = "Enter custom value"):
        super().__init__(name, display_name, default_value, description)
        self.options = options if options else []
        self.internal_value = default_value
        self.dropdown = None
        self.allow_custom = allow_custom
        self.custom_placeholder = custom_placeholder
        self.custom_input = None
        self.custom_input_container = None

    def render_to_layout(self, layout: QVBoxLayout):
        row_layout = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(self.display_name)
        label.setStyleSheet(f"font-size: 16px; color: {'#ffffff' if colorMode=='dark' else '#333333'};")
        row_layout.addWidget(label)
        self.dropdown = QtWidgets.QComboBox()
        self.dropdown.setStyleSheet(f"""
            font-size: 16px;
            padding: 5px;
            background-color: {'#444' if colorMode=='dark' else 'white'};
            color: {'#ffffff' if colorMode=='dark' else '#000000'};
            border: 1px solid {'#666' if colorMode=='dark' else '#ccc'};
        """)

        # Add preset options
        for option, value in self.options:
            self.dropdown.addItem(option, value)

        # Add "Custom" option if enabled
        if self.allow_custom:
            self.dropdown.addItem("🔧 Custom", self._CUSTOM_SENTINEL)

        # Set initial selection based on internal_value
        index = self.dropdown.findData(self.internal_value)
        if index != -1:
            # Value matches a preset option
            self.dropdown.setCurrentIndex(index)
        elif self.allow_custom and self.internal_value:
            # Value doesn't match any preset - it's a custom value, select "Custom"
            custom_index = self.dropdown.findData(self._CUSTOM_SENTINEL)
            if custom_index != -1:
                self.dropdown.setCurrentIndex(custom_index)

        row_layout.addWidget(self.dropdown)
        layout.addLayout(row_layout)

        # Create custom input row if allow_custom is enabled
        if self.allow_custom:
            self.custom_input_container = QtWidgets.QWidget()
            custom_row_layout = QtWidgets.QHBoxLayout(self.custom_input_container)
            custom_row_layout.setContentsMargins(0, 5, 0, 0)

            self.custom_input = QtWidgets.QLineEdit()
            self.custom_input.setPlaceholderText(self.custom_placeholder)
            self.custom_input.setStyleSheet(f"""
                font-size: 16px;
                padding: 5px;
                background-color: {'#444' if colorMode=='dark' else 'white'};
                color: {'#ffffff' if colorMode=='dark' else '#000000'};
                border: 1px solid {'#666' if colorMode=='dark' else '#ccc'};
            """)

            # If current value is custom (not in presets), populate the input
            if self.dropdown.currentData() == self._CUSTOM_SENTINEL and self.internal_value:
                self.custom_input.setText(self.internal_value)

            custom_row_layout.addWidget(self.custom_input)
            layout.addWidget(self.custom_input_container)

            # Connect signal to show/hide custom input when dropdown changes
            self.dropdown.currentIndexChanged.connect(self._on_dropdown_changed)
            # Set initial visibility
            self._update_custom_input_visibility()

    def _on_dropdown_changed(self):
        """Handle dropdown selection change to show/hide custom input."""
        self._update_custom_input_visibility()

    def _update_custom_input_visibility(self):
        """Show or hide the custom input based on dropdown selection."""
        if self.custom_input_container and self.dropdown:
            is_custom = self.dropdown.currentData() == self._CUSTOM_SENTINEL
            self.custom_input_container.setVisible(is_custom)
            # Focus the input when switching to Custom for better UX
            if is_custom and self.custom_input:
                self.custom_input.setFocus()

    def set_value(self, value):
        self.internal_value = value

    def get_value(self):
        # If "Custom" is selected, return the text input value (stripped of whitespace)
        if self.allow_custom and self.dropdown and self.dropdown.currentData() == self._CUSTOM_SENTINEL:
            return self.custom_input.text().strip() if self.custom_input else ""
        return self.dropdown.currentData() if self.dropdown else (self.internal_value or "")


class AIProvider(ABC):
    """
    Abstract base class for AI providers.
    
    All providers must implement:
      • get_response(system_instruction, prompt) -> str
      • after_load() to create their client or model instance
      • before_load() to cleanup any existing client
      • cancel() to cancel an ongoing request
    """
    def __init__(self, app, provider_name: str, settings: Sequence[AIProviderSetting],
                 description: str = "An unfinished AI provider!",
                 logo: str = "generic",
                 button_text: str = "Go to URL",
                 button_action: Any = None):
        self.provider_name = provider_name
        self.settings = settings
        self.app = app
        self.description = description if description else "An unfinished AI provider!"
        self.logo = logo
        self.button_text = button_text
        self.button_action = button_action

    @abstractmethod
    def get_response(self, system_instruction: str, prompt: str) -> str:
        """
        Send the given system instruction and prompt to the AI provider and return the full response text.
        """
        pass

    def load_config(self, config: dict):
        """
        Load configuration settings into the provider.
        """
        for setting in self.settings:
            if setting.name in config:
                setattr(self, setting.name, config[setting.name])
                setting.set_value(config[setting.name])
            else:
                setattr(self, setting.name, setting.default_value)
        self.after_load()

    def save_config(self):
        """
        Save provider configuration settings into the main config file.
        """
        config = {}
        for setting in self.settings:
            config[setting.name] = setting.get_value()
        self.app.config["providers"][self.provider_name] = config
        self.app.save_config(self.app.config)

    @abstractmethod
    def after_load(self):
        """
        Called after configuration is loaded; create your API client here.
        """
        pass

    @abstractmethod
    def before_load(self):
        """
        Called before reloading configuration; cleanup your API client here.
        """
        pass

    @abstractmethod
    def cancel(self):
        """
        Cancel any ongoing API request.
        """
        pass


class GeminiProvider(AIProvider):
    """
    Provider for Google's Gemini API (using the new unified `google-genai` SDK).
    """
    api_key: str = ""
    model_name: str = "gemini-3.1-flash-lite"

    # Disable safety filtering across all categories (best-effort; some models
    # may still soft-refuse). Defined once, reused per request.
    _SAFETY_SETTINGS = [
        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]

    def __init__(self, app):
        self.close_requested = False
        self.client = None

        settings = [
            TextSetting(name="api_key", display_name="API Key", description="Paste your Gemini API key here"),
            DropdownSetting(
                name="model_name",
                display_name="Model",
                default_value="gemini-3.1-flash-lite",
                description="Select Gemini model to use",
                options=[
                    ("⭐ Gemini 3.1 Flash-Lite (Recommended | fast & high limits)", "gemini-3.1-flash-lite"),
                    ("Gemini Flash Latest (very fast | 20 free uses/day)", "gemini-flash-latest"),
                    ("Gemma 4 31B (slow | unlimited free use)", "gemma-4-31b-it"),
                    ("Gemma 4 26B A4B (slow | unlimited free use)", "gemma-4-26b-a4b-it"),
                ],
                allow_custom=True,
                custom_placeholder="e.g., gemini-3.1-pro-preview"
            )
        ]
        super().__init__(app, "Gemini (Recommended)", settings,
            "• Google's Gemini is a powerful AI model available for free!\n"
            "• An API key is required to connect to Gemini on your behalf.\n"
            "• Click the button below to get your API key.",
            "gemini",
            "Get API Key",
            lambda: webbrowser.open("https://aistudio.google.com/app/apikey"))

    def _build_config(self, system_instruction: str) -> "genai_types.GenerateContentConfig":
        is_gemma = "gemma" in (getattr(self, 'model_name', '') or "").lower()
        kwargs = {
            "system_instruction": system_instruction,
            "safety_settings": self._SAFETY_SETTINGS,
            "max_output_tokens": 1000,
        }
        if not is_gemma:
            kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_level="minimal")  # type: ignore
        return genai_types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _messages_to_contents(messages: list) -> list:
        contents = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            text = m.get("content", "")
            gemini_role = "model" if role == "assistant" else "user"
            contents.append(genai_types.Content(role=gemini_role, parts=[genai_types.Part(text=text)]))
        return contents

    def get_response(self, system_instruction: str, prompt, return_response: bool = False) -> str:
        self.close_requested = False
        if not self.client:
            return ""

        try:
            contents = self._messages_to_contents(prompt) if isinstance(prompt, list) else prompt

            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=self._build_config(system_instruction),
            )

            response_text = (response.text or "").rstrip('\n')

            if not return_response and not hasattr(self.app, 'current_response_window'):
                self.app.output_ready_signal.emit(response_text)
                self.app.replace_text(True)
                return ""
            return response_text
        except Exception as e:
            logging.error(f"Error processing Gemini response: {e}")
            self.app.output_ready_signal.emit("An error occurred while processing the response.")
            return ""
        finally:
            self.close_requested = False

    def load_config(self, config: dict):
        if 'api_key' in config:
            config = config.copy()
            config['api_key'] = deobfuscate_api_key(config['api_key'])
        super().load_config(config)

    def save_config(self):
        config = {}
        for setting in self.settings:
            value = setting.get_value()
            if setting.name == 'api_key' and isinstance(value, str):
                value = obfuscate_api_key(value)
            config[setting.name] = value
        self.app.config["providers"][self.provider_name] = config
        self.app.save_config(self.app.config)

    def after_load(self):
        """
        Construct the new `google-genai` Client. The model and per-call options
        are passed at request time via `client.models.generate_content`, so we
        don't pre-instantiate a model here.
        """
        self.client = genai.Client(api_key=self.api_key)

    def before_load(self):
        self.client = None

    def cancel(self):
        self.close_requested = True


class OpenAICompatibleProvider(AIProvider):
    """
    Provider for OpenAI-compatible APIs.
    """
    api_key: str = ""
    api_base: str = ""
    api_organisation: str = ""
    api_project: str = ""
    api_model: str = ""

    def __init__(self, app):
        self.close_requested = None
        self.client = None

        settings = [
            TextSetting(name="api_key", display_name="API Key", description="API key for the OpenAI-compatible API."),
            TextSetting("api_base", "API Base URL", "https://api.openai.com/v1", "E.g. https://api.openai.com/v1"),
            TextSetting("api_organisation", "API Organisation", "", "Leave blank if not applicable."),
            TextSetting("api_project", "API Project", "", "Leave blank if not applicable."),
            TextSetting("api_model", "API Model", "gpt-4o-mini", "E.g. gpt-4o-mini"),
        ]
        super().__init__(app, "OpenAI Compatible (For Experts)", settings,
            "• Connect to ANY OpenAI-compatible API (v1/chat/completions).\n"
            "• You must abide by the service's Terms of Service.",
            "openai", "Get OpenAI API Key", lambda: webbrowser.open("https://platform.openai.com/account/api-keys"))

    def get_response(self, system_instruction: str, prompt: str | list, return_response: bool = False) -> str:
        self.close_requested = False
        if not self.client:
            return ""

        if isinstance(prompt, list):
            messages = prompt
        else:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ]

        try:
            response = self.client.chat.completions.create(
                model=self.api_model,
                messages=messages,
                temperature=0.5,
                stream=False
            )
            response_text = (response.choices[0].message.content or "").strip()

            if not return_response and not hasattr(self.app, 'current_response_window'):
                self.app.output_ready_signal.emit(response_text)
            return response_text

        except Exception as e:
            error_str = str(e)
            logging.error(f"Error while generating content: {error_str}")
            if "exceeded" in error_str or "rate limit" in error_str:
                self.app.show_message_signal.emit(
                    "Rate Limit Hit",
                    "It appears you have hit an API rate/usage limit. Please try again later or adjust your settings."
                )
            else:
                self.app.show_message_signal.emit("Error", f"An error occurred: {error_str}")
            return ""

    def after_load(self):
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
            organization=self.api_organisation,
            project=self.api_project
        )

    def before_load(self):
        self.client = None

    def cancel(self):
        self.close_requested = True


class OllamaProvider(AIProvider):
    """
    Provider for connecting to an Ollama server.
    """
    api_base: str = ""
    api_model: str = ""
    keep_alive: str = "5"

    def __init__(self, app):
        self.close_requested = None
        self.client = None
        self.app = app
        settings = [
            TextSetting("api_base", "API Base URL", "http://localhost:11434", "E.g. http://localhost:11434"),
            TextSetting("api_model", "API Model", "llama3.1:8b", "E.g. llama3.1:8b"),
            TextSetting("keep_alive", "Time to keep the model loaded in memory in minutes", "5", "E.g. 5")
        ]
        super().__init__(app, "Ollama (For Experts)", settings,
            "• Connect to an Ollama server (local LLM).",
            "ollama", "Ollama Set-up Instructions",
            lambda: webbrowser.open("https://github.com/theJayTea/WritingTools?tab=readme-ov-file#-optional-ollama-local-llm-instructions-for-windows-v7-onwards"))

    def get_response(self, system_instruction: str, prompt: str | list, return_response: bool = False) -> str:
        self.close_requested = False
        if not self.client:
            return ""

        if isinstance(prompt, list):
            messages = prompt
        else:
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ]

        try:
            response = self.client.chat(model=self.api_model, messages=messages)
            response_text = response['message']['content'].strip()
            if not return_response and not hasattr(self.app, 'current_response_window'):
                self.app.output_ready_signal.emit(response_text)
            return response_text
        except Exception as e:
            logging.error(f"Error during Ollama chat: {e}")
            self.app.output_ready_signal.emit("An error occurred during Ollama chat.")
            return ""

    def after_load(self):
        self.client = OllamaClient(host=self.api_base)

    def before_load(self):
        self.client = None

    def cancel(self):
        self.close_requested = True
