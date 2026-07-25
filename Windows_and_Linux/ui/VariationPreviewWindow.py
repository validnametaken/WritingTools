import logging
import os
import sys
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.UIUtils import ThemeBackground, colorMode


class VariationCard(QWidget):
    """
    A card widget displaying a single variation option.
    Clicking the card or its selection button chooses this variation.
    """
    selected = QtCore.Signal(str)

    def __init__(self, index, label_text, variation_text, parent=None):
        super().__init__(parent)
        self.variation_text = variation_text
        self.setMouseTracking(True)
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
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

        apply_btn = QPushButton("Apply & Paste")
        apply_btn.setCursor(Qt.PointingHandCursor)
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

        # Content text display
        text_display = QPlainTextEdit()
        text_display.setPlainText(self.variation_text)
        text_display.setReadOnly(True)
        text_display.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: transparent;
                color: {'#E0E0E0' if colorMode == 'dark' else '#212121'};
                border: none;
                font-size: 13px;
                line-height: 1.4;
            }}
        """)

        # Auto-adjust height based on document height up to max
        doc = text_display.document()
        doc.adjustSize()
        h = int(doc.size().height()) + 20
        text_display.setFixedHeight(min(max(h, 60), 160))

        layout.addWidget(text_display)

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

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_click()
        super().mousePressEvent(event)

    def _on_click(self):
        self.selected.emit(self.variation_text)


class VariationPreviewWindow(QDialog):
    """
    Popup dialog presenting 3 LLM-generated writing variations to the user.
    """
    def __init__(self, parent=None, original_text="", variations=None):
        super().__init__(parent)
        self.original_text = original_text
        self.variations = variations or []
        self.selected_variation = None

        self.init_ui()

    def init_ui(self):
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Writing Tools - Select Variation")
        self.resize(520, 580)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.background = ThemeBackground(
            self,
            theme='gradient',
            is_popup=True,
            border_radius=12
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
        top_bar.addWidget(title_label, 1, Qt.AlignLeft | Qt.AlignVCenter)

        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
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
        top_bar.addWidget(close_btn, 0, Qt.AlignRight)
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
        orig_display.setMaximumHeight(80)
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

        # Variations List
        var_header = QLabel("Choose a Variation (or press 1, 2, 3):")
        var_header.setStyleSheet(f"""
            QLabel {{
                color: {'#AAAAAA' if colorMode == 'dark' else '#666666'};
                font-size: 12px;
                font-weight: bold;
                margin-top: 4px;
            }}
        """)
        content_layout.addWidget(var_header)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)

        for idx, var_info in enumerate(self.variations):
            lbl = var_info.get("label", f"Variation {idx + 1}")
            txt = var_info.get("text", "")
            card = VariationCard(idx, lbl, txt, parent=scroll_content)
            card.selected.connect(self._on_variation_selected)
            scroll_layout.addWidget(card)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        content_layout.addWidget(scroll_area, 1)

        # Footer Bar: Cancel / Dismiss button
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 4, 0, 0)

        cancel_btn = QPushButton("Cancel / Dismiss (Esc)")
        cancel_btn.setCursor(Qt.PointingHandCursor)
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
        footer_layout.addStretch()
        footer_layout.addWidget(cancel_btn)

        content_layout.addLayout(footer_layout)

    def _on_variation_selected(self, text):
        self.selected_variation = text
        self.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_1, Qt.Key_Key_1) and len(self.variations) >= 1:
            self._on_variation_selected(self.variations[0]["text"])
        elif key in (Qt.Key_2, Qt.Key_Key_2) and len(self.variations) >= 2:
            self._on_variation_selected(self.variations[1]["text"])
        elif key in (Qt.Key_3, Qt.Key_Key_3) and len(self.variations) >= 3:
            self._on_variation_selected(self.variations[2]["text"])
        elif key == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)
