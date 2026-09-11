import difflib
import html
import logging
import os
import re
import sys
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.UIUtils import colorMode


def generate_word_diff_html(original_text: str, variation_text: str) -> str:
    """
    Generates clean word-level HTML diff output.
    Insertions are highlighted in green, deletions in strikethrough red.
    """
    if not original_text:
        return html.escape(variation_text).replace('\n', '<br>')

    # Tokenize preserving words, punctuation, and whitespace
    def tokenize(text):
        return re.findall(r'\s+|\w+|[^\w\s]', text, re.UNICODE)

    orig_tokens = tokenize(original_text)
    var_tokens = tokenize(variation_text)

    matcher = difflib.SequenceMatcher(None, orig_tokens, var_tokens)
    out_html = []

    is_dark = colorMode == 'dark'
    ins_style = (
        'background-color: #1b4d2e; color: #a5d6a7; padding: 1px 3px; border-radius: 3px; font-weight: bold;'
        if is_dark else
        'background-color: #d4edda; color: #155724; padding: 1px 3px; border-radius: 3px; font-weight: bold;'
    )
    del_style = (
        'background-color: #4d1b1b; color: #ef9a9a; text-decoration: line-through; padding: 1px 3px; border-radius: 3px;'
        if is_dark else
        'background-color: #f8d7da; color: #721c24; text-decoration: line-through; padding: 1px 3px; border-radius: 3px;'
    )

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            chunk = "".join(var_tokens[j1:j2])
            out_html.append(html.escape(chunk).replace('\n', '<br>'))
        elif tag == 'insert':
            chunk = "".join(var_tokens[j1:j2])
            escaped = html.escape(chunk).replace('\n', '<br>')
            out_html.append(f'<span style="{ins_style}">{escaped}</span>')
        elif tag == 'delete':
            chunk = "".join(orig_tokens[i1:i2])
            escaped = html.escape(chunk).replace('\n', '<br>')
            out_html.append(f'<span style="{del_style}">{escaped}</span>')
        elif tag == 'replace':
            del_chunk = "".join(orig_tokens[i1:i2])
            ins_chunk = "".join(var_tokens[j1:j2])
            del_escaped = html.escape(del_chunk).replace('\n', '<br>')
            ins_escaped = html.escape(ins_chunk).replace('\n', '<br>')
            out_html.append(f'<span style="{del_style}">{del_escaped}</span> <span style="{ins_style}">{ins_escaped}</span>')

    return "".join(out_html)


class VariationCard(QWidget):
    """
    A card widget displaying a single variation option with optional diff rendering.
    """
    selected = Signal(str)

    def __init__(self, index, label_text, variation_text, original_text="", show_diff=False, parent=None):
        super().__init__(parent)
        self.variation_text = variation_text
        self.original_text = original_text
        self.show_diff = show_diff
        self.setMouseTracking(True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.init_ui(index, label_text)

    def init_ui(self, index, label_text):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header: Label (e.g. "Variation 1: Concise") and Apply Button
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)

        badge_text = f"[{index + 1}] {label_text}"
        badge_label = QLabel(badge_text)
        badge_label.setStyleSheet(f"""
            QLabel {{
                font-weight: bold;
                font-size: 13px;
                color: {'#64B5F6' if colorMode == 'dark' else '#1976D2'};
            }}
        """)
        header_layout.addWidget(badge_label)
        header_layout.addStretch()

        # Copy to Clipboard button
        copy_btn = QPushButton("Copy")
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {'#424242' if colorMode == 'dark' else '#E0E0E0'};
                color: {'#FFFFFF' if colorMode == 'dark' else '#333333'};
                border: 1px solid {'#555555' if colorMode == 'dark' else '#CCCCCC'};
                border-radius: 5px;
                padding: 4px 8px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {'#616161' if colorMode == 'dark' else '#D5D5D5'};
            }}
        """)
        copy_btn.clicked.connect(self._on_copy_clicked)
        header_layout.addWidget(copy_btn)

        apply_btn = QPushButton("Apply & Paste")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {'#2e7d32' if colorMode == 'dark' else '#4CAF50'};
                color: white;
                border: none;
                border-radius: 5px;
                padding: 4px 10px;
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {'#1b5e20' if colorMode == 'dark' else '#45a049'};
            }}
        """)
        apply_btn.clicked.connect(self._on_click)
        header_layout.addWidget(apply_btn)

        layout.addLayout(header_layout)

        # Content text display (QTextEdit supports both HTML diff and plain text)
        self.text_display = QTextEdit()
        self.text_display.setReadOnly(True)
        self.text_display.setStyleSheet(f"""
            QTextEdit {{
                background-color: transparent;
                color: {'#E0E0E0' if colorMode == 'dark' else '#212121'};
                border: none;
                font-size: 13px;
                line-height: 1.4;
            }}
        """)

        self._update_text_display()
        layout.addWidget(self.text_display)

        # Main Card Styling
        self.setStyleSheet(f"""
            VariationCard {{
                background-color: {'#2D2D2D' if colorMode == 'dark' else '#FFFFFF'};
                border: 1px solid {'#444444' if colorMode == 'dark' else '#E0E0E0'};
                border-radius: 8px;
            }}
            VariationCard:hover {{
                border: 1.5px solid {'#64B5F6' if colorMode == 'dark' else '#1976D2'};
            }}
        """)

    def set_show_diff(self, show_diff):
        self.show_diff = show_diff
        self._update_text_display()

    def _update_text_display(self):
        if self.show_diff:
            diff_html = generate_word_diff_html(self.original_text, self.variation_text)
            self.text_display.setHtml(diff_html)
        else:
            self.text_display.setPlainText(self.variation_text)

        doc = self.text_display.document()
        doc.adjustSize()
        h = int(doc.size().height()) + 20
        self.text_display.setFixedHeight(min(max(h, 60), 220))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mousePressEvent(event)

    def _on_copy_clicked(self):
        import pyperclip
        pyperclip.copy(self.variation_text)
        sender = self.sender()
        if isinstance(sender, QPushButton):
            orig_text = sender.text()
            sender.setText("✓ Copied")
            QtCore.QTimer.singleShot(1200, lambda: sender.setText(orig_text))

    def _on_click(self):
        self.selected.emit(self.variation_text)


class VariationPreviewWindow(QDialog):
    """
    Popup dialog presenting 3 LLM-generated writing variations to the user,
    with live refinement/regeneration and visual diff highlighting.
    """
    refinement_requested = Signal(str, str)  # (original_text, refinement_instruction)

    def __init__(self, parent=None, original_text="", variations=None, app=None):
        super().__init__(parent)
        self.app = app
        self.original_text = original_text
        self.variations = variations or []
        self.selected_variation = None
        self.show_diff = False
        self.card_widgets = []
        self._loading = len(self.variations) == 0
        self._pulse_timer = None
        self._pulse_alpha = 0
        self._pulse_direction = 1
        self._loading_cards = []

        self.init_ui()

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setWindowTitle("Writing Tools - Select Variation")

        saved_size = None
        if self.app and hasattr(self.app, 'config') and self.app.config:
            saved_size = self.app.config.get('window_sizes', {}).get('VariationPreviewWindow')
        
        if saved_size and len(saved_size) == 2:
            self.resize(saved_size[0], saved_size[1])
        else:
            self.resize(540, 640)

        self.setMinimumSize(460, 500)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Plain container widget — ThemeBackground requires a frameless/transparent
        # window to look correct, so we use a simple styled QWidget here instead.
        self.background = QWidget(self)
        self.background.setObjectName("VPWBackground")
        is_dark = colorMode == 'dark'
        self.background.setStyleSheet(
            "QWidget#VPWBackground { "
            f"background-color: {'#1E1E1E' if is_dark else '#F5F5F5'}; "
            "}"
        )
        main_layout.addWidget(self.background)

        content_layout = QVBoxLayout(self.background)
        content_layout.setContentsMargins(14, 10, 14, 14)
        content_layout.setSpacing(10)

        # Header Bar
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        title_label = QLabel("Select Writing Variation")
        title_label.setStyleSheet(f"""
            QLabel {{
                color: {'#FFFFFF' if colorMode == 'dark' else '#212121'};
                font-size: 16px;
                font-weight: bold;
            }}
        """)
        top_bar.addWidget(title_label, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Show Diff Toggle Checkbox
        self.diff_checkbox = QCheckBox("Show Diff")
        self.diff_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.diff_checkbox.setStyleSheet(f"""
            QCheckBox {{
                color: {'#64B5F6' if colorMode == 'dark' else '#1976D2'};
                font-size: 12px;
                font-weight: bold;
            }}
        """)
        self.diff_checkbox.toggled.connect(self._on_diff_toggled)
        top_bar.addWidget(self.diff_checkbox, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {'#FFFFFF' if colorMode == 'dark' else '#333333'};
                font-size: 20px;
                font-weight: bold;
                border: none;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: {'#444444' if colorMode == 'dark' else '#EBEBEB'};
            }}
        """)
        close_btn.clicked.connect(self.reject)
        top_bar.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)
        content_layout.addLayout(top_bar)

        # Original Text Context
        orig_label = QLabel("Original Text:")
        orig_label.setStyleSheet(f"""
            QLabel {{
                color: {'#AAAAAA' if colorMode == 'dark' else '#666666'};
                font-size: 12px;
                font-weight: bold;
            }}
        """)
        content_layout.addWidget(orig_label)

        orig_display = QPlainTextEdit()
        orig_display.setPlainText(self.original_text)
        orig_display.setReadOnly(True)
        orig_display.setMaximumHeight(70)
        orig_display.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {'#252525' if colorMode == 'dark' else '#F5F5F5'};
                color: {'#CCCCCC' if colorMode == 'dark' else '#444444'};
                border: 1px solid {'#3D3D3D' if colorMode == 'dark' else '#DDD'};
                border-radius: 6px;
                padding: 6px;
                font-size: 12px;
            }}
        """)
        content_layout.addWidget(orig_display)

        # Variations List Header
        var_header = QLabel("Choose a Variation (or press 1, 2, 3):")
        var_header.setStyleSheet(f"""
            QLabel {{
                color: {'#AAAAAA' if colorMode == 'dark' else '#666666'};
                font-size: 12px;
                font-weight: bold;
                margin-top: 2px;
            }}
        """)
        content_layout.addWidget(var_header)

        # Scroll Area for Cards
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(10)

        self.scroll_area.setWidget(self.scroll_content)
        content_layout.addWidget(self.scroll_area, 1)

        # Live Refinement Input Area
        refine_layout = QHBoxLayout()
        refine_layout.setContentsMargins(0, 4, 0, 0)
        refine_layout.setSpacing(6)

        self.refine_input = QLineEdit()
        self.refine_input.setPlaceholderText("Refine variations (e.g. 'make option 1 shorter')...")
        self.refine_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 7px;
                border: 1px solid {'#555555' if colorMode == 'dark' else '#CCCCCC'};
                border-radius: 6px;
                background-color: {'#2A2A2A' if colorMode == 'dark' else '#FFFFFF'};
                color: {'#FFFFFF' if colorMode == 'dark' else '#000000'};
                font-size: 12px;
            }}
        """)
        self.refine_input.returnPressed.connect(self._on_refine_clicked)
        refine_layout.addWidget(self.refine_input, 1)

        self.regen_btn = QPushButton("Regenerate")
        self.regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regen_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {'#0288D1' if colorMode == 'dark' else '#0288D1'};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 7px 14px;
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {'#01579B' if colorMode == 'dark' else '#01579B'};
            }}
        """)
        self.regen_btn.clicked.connect(self._on_refine_clicked)
        refine_layout.addWidget(self.regen_btn)

        content_layout.addLayout(refine_layout)

        # Footer Bar: Status & Cancel / Dismiss button
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 2, 0, 0)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"""
            QLabel {{
                color: {'#81C784' if colorMode == 'dark' else '#2E7D32'};
                font-size: 12px;
                font-weight: bold;
            }}
        """)
        footer_layout.addWidget(self.status_label, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        cancel_btn = QPushButton("Cancel / Dismiss (Esc)")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {'#3A3A3A' if colorMode == 'dark' else '#E0E0E0'};
                color: {'#FFFFFF' if colorMode == 'dark' else '#333333'};
                border: 1px solid {'#555555' if colorMode == 'dark' else '#CCCCCC'};
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {'#4A4A4A' if colorMode == 'dark' else '#D0D0D0'};
            }}
        """)
        cancel_btn.clicked.connect(self.reject)
        footer_layout.addWidget(cancel_btn, 0, Qt.AlignmentFlag.AlignRight)

        content_layout.addLayout(footer_layout)

        # Render cards or loading placeholders once all widgets (including status_label) exist
        if self._loading:
            self._render_loading_state()
        else:
            self._render_cards()

    def _render_loading_state(self):
        """Show placeholder loading cards with a smooth pulsing shimmer animation."""
        is_dark = colorMode == 'dark'
        self._loading_cards.clear()

        for i in range(3):
            card = QWidget(self.scroll_content)
            card.setStyleSheet(
                f"background-color: {'#2A2A2A' if is_dark else '#EEEEEE'};"
                f"border: 1px solid {'#3D3D3D' if is_dark else '#DDDDDD'};"
                "border-radius: 8px; padding: 14px;"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            spinner = QLabel(f"Option {i + 1}  •  Generating variation...")
            spinner.setStyleSheet(
                f"color: {'#888888' if is_dark else '#777777'}; font-size: 13px; font-style: italic; font-weight: 500;"
            )
            card_layout.addWidget(spinner)
            self.scroll_layout.addWidget(card)
            self._loading_cards.append(card)

        self.scroll_layout.addStretch()
        self.status_label.setText("Generating variations...")
        self.regen_btn.setEnabled(False)
        self.refine_input.setEnabled(False)

        # Start pulsing shimmer animation timer
        self._pulse_alpha = 0
        self._pulse_direction = 1
        if self._pulse_timer is None:
            self._pulse_timer = QtCore.QTimer(self)
            self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_timer.start(50)

    def _on_pulse_tick(self):
        if not self._loading or not self._loading_cards:
            if self._pulse_timer and self._pulse_timer.isActive():
                self._pulse_timer.stop()
            return

        self._pulse_alpha += 4 * self._pulse_direction
        if self._pulse_alpha >= 40:
            self._pulse_direction = -1
        elif self._pulse_alpha <= 0:
            self._pulse_direction = 1

        is_dark = colorMode == 'dark'
        base_bg = 42 + self._pulse_alpha if is_dark else 238 - self._pulse_alpha
        border_hex = f"#{60 + self._pulse_alpha:02x}{60 + self._pulse_alpha:02x}{60 + self._pulse_alpha:02x}" if is_dark else f"#{200 - self._pulse_alpha:02x}{200 - self._pulse_alpha:02x}{200 - self._pulse_alpha:02x}"
        bg_hex = f"#{base_bg:02x}{base_bg:02x}{base_bg:02x}"

        for card in self._loading_cards:
            card.setStyleSheet(f"background-color: {bg_hex}; border: 1px solid {border_hex}; border-radius: 8px; padding: 14px;")

    def _render_cards(self):
        # Clear existing cards
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.deleteLater()

        self.card_widgets.clear()
        self._loading_cards.clear()

        # Render already-completed variations
        for idx, var_info in enumerate(self.variations):
            lbl = var_info.get("label", f"Variation {idx + 1}")
            txt = var_info.get("text", "")
            card = VariationCard(
                idx, lbl, txt,
                original_text=self.original_text,
                show_diff=self.show_diff,
                parent=self.scroll_content
            )
            card.selected.connect(self._on_variation_selected)
            self.scroll_layout.addWidget(card)
            self.card_widgets.append(card)

        # If still loading and fewer than 3 variations are ready, render placeholders for remaining slots
        if self._loading and len(self.variations) < 3:
            is_dark = colorMode == 'dark'
            for idx in range(len(self.variations), 3):
                card = QWidget(self.scroll_content)
                card.setStyleSheet(
                    f"background-color: {'#2A2A2A' if is_dark else '#EEEEEE'};"
                    f"border: 1px solid {'#3D3D3D' if is_dark else '#DDDDDD'};"
                    "border-radius: 8px; padding: 14px;"
                )
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(10, 8, 10, 8)
                spinner = QLabel(f"Option {idx + 1}  •  Generating variation...")
                spinner.setStyleSheet(
                    f"color: {'#888888' if is_dark else '#777777'}; font-size: 13px; font-style: italic; font-weight: 500;"
                )
                card_layout.addWidget(spinner)
                self.scroll_layout.addWidget(card)
                self._loading_cards.append(card)

            if self._pulse_timer and not self._pulse_timer.isActive():
                self._pulse_timer.start(50)

        self.scroll_layout.addStretch()

    def _on_diff_toggled(self, checked):
        self.show_diff = checked
        for card in self.card_widgets:
            card.set_show_diff(checked)

    def _on_refine_clicked(self):
        text = self.refine_input.text().strip()
        if not text:
            return
        self.set_loading_state(True)
        self.refinement_requested.emit(self.original_text, text)

    @Slot(list)
    def update_variations(self, new_variations):
        """
        Updates cards with new variations. When all 3 are ready, restores idle state.
        """
        if len(new_variations) >= 3:
            self._loading = False
            if self._pulse_timer and self._pulse_timer.isActive():
                self._pulse_timer.stop()
            self._loading_cards.clear()
            self.set_loading_state(False)
            self.refine_input.clear()

        self.variations = new_variations
        self._render_cards()

    def set_loading_state(self, is_loading):
        self.regen_btn.setEnabled(not is_loading)
        self.refine_input.setEnabled(not is_loading)
        if is_loading:
            self.status_label.setText("Generating new variations...")
        else:
            self.status_label.setText("")

    def _on_variation_selected(self, text):
        self.selected_variation = text
        self.accept()

    def keyPressEvent(self, event):
        key = event.key()
        # Return / Enter key directly selects Option 1 (if not typing in the refinement box)
        if (key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter) and not self.refine_input.hasFocus() and len(self.variations) >= 1:
            self._on_variation_selected(self.variations[0]["text"])
        elif key == Qt.Key.Key_1 and len(self.variations) >= 1:
            self._on_variation_selected(self.variations[0]["text"])
        elif key == Qt.Key.Key_2 and len(self.variations) >= 2:
            self._on_variation_selected(self.variations[1]["text"])
        elif key == Qt.Key.Key_3 and len(self.variations) >= 3:
            self._on_variation_selected(self.variations[2]["text"])
        elif key == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def _persist_geometry(self):
        if self.isMinimized() or self.isMaximized():
            return
        if hasattr(self, 'app') and self.app and hasattr(self.app, 'save_window_geometry'):
            pos = self.pos()
            size = self.size()
            self.app.save_window_geometry('VariationPreviewWindow', pos.x(), pos.y(), size.width(), size.height())

    def _schedule_save_geometry(self):
        if not hasattr(self, '_geom_save_timer'):
            self._geom_save_timer = QtCore.QTimer(self)
            self._geom_save_timer.setSingleShot(True)
            self._geom_save_timer.timeout.connect(self._persist_geometry)
        self._geom_save_timer.start(300)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_save_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_save_geometry()

    def accept(self):
        self._persist_geometry()
        super().accept()

    def reject(self):
        self._persist_geometry()
        if self._pulse_timer and self._pulse_timer.isActive():
            self._pulse_timer.stop()
        if self.app and hasattr(self.app, 'current_provider') and self.app.current_provider and hasattr(self.app.current_provider, 'cancel'):
            self.app.current_provider.cancel()
        super().reject()

    def closeEvent(self, event):
        self._persist_geometry()
        if self._pulse_timer and self._pulse_timer.isActive():
            self._pulse_timer.stop()
        if self.app and hasattr(self.app, 'current_provider') and self.app.current_provider and hasattr(self.app.current_provider, 'cancel'):
            self.app.current_provider.cancel()
        super().closeEvent(event)
