"""Dark/light QSS themes for Accelerated Downloader Pro."""

LIGHT_QSS = """
QWidget { background-color: #f5f6f8; color: #202124; font-size: 13px; }
QMainWindow, QDialog { background-color: #f5f6f8; }
QListWidget { background-color: #ffffff; border: 1px solid #dcdfe4; border-radius: 6px; }
QListWidget::item { border-bottom: 1px solid #eceef1; }
QListWidget::item:selected { background-color: #e3edff; }
QLineEdit, QSpinBox, QComboBox, QDateTimeEdit {
    background-color: #ffffff; border: 1px solid #c7cbd1; border-radius: 4px; padding: 4px 6px;
}
QPushButton {
    background-color: #2563eb; color: white; border: none; border-radius: 5px; padding: 6px 14px;
}
QPushButton:hover { background-color: #1d4ed8; }
QPushButton:pressed { background-color: #1e40af; }
QPushButton:disabled { background-color: #a9b6cc; }
QPushButton#secondary {
    background-color: #ffffff; color: #202124; border: 1px solid #c7cbd1;
}
QPushButton#secondary:hover { background-color: #eef1f5; }
QProgressBar {
    border: 1px solid #c7cbd1; border-radius: 5px; text-align: center; background: #eceef1; height: 14px;
}
QProgressBar::chunk { background-color: #2563eb; border-radius: 5px; }
QStatusBar { background-color: #eceef1; }
QLabel#categoryBadge {
    background-color: #e3edff; color: #1d4ed8; border-radius: 8px; padding: 1px 8px; font-size: 11px;
}
QToolBar { background: #eceef1; border: none; spacing: 6px; padding: 4px; }
"""

DARK_QSS = """
QWidget { background-color: #1e1f22; color: #e4e6eb; font-size: 13px; }
QMainWindow, QDialog { background-color: #1e1f22; }
QListWidget { background-color: #26282c; border: 1px solid #35373c; border-radius: 6px; }
QListWidget::item { border-bottom: 1px solid #2f3136; }
QListWidget::item:selected { background-color: #30425f; }
QLineEdit, QSpinBox, QComboBox, QDateTimeEdit {
    background-color: #2b2d31; border: 1px solid #40434a; border-radius: 4px; padding: 4px 6px; color: #e4e6eb;
}
QPushButton {
    background-color: #3b82f6; color: white; border: none; border-radius: 5px; padding: 6px 14px;
}
QPushButton:hover { background-color: #2f6fe0; }
QPushButton:pressed { background-color: #2557b8; }
QPushButton:disabled { background-color: #4b5563; }
QPushButton#secondary {
    background-color: #2b2d31; color: #e4e6eb; border: 1px solid #40434a;
}
QPushButton#secondary:hover { background-color: #35373c; }
QProgressBar {
    border: 1px solid #40434a; border-radius: 5px; text-align: center; background: #2b2d31; height: 14px;
    color: #e4e6eb;
}
QProgressBar::chunk { background-color: #3b82f6; border-radius: 5px; }
QStatusBar { background-color: #26282c; color: #e4e6eb; }
QLabel#categoryBadge {
    background-color: #30425f; color: #8ab4f8; border-radius: 8px; padding: 1px 8px; font-size: 11px;
}
QToolBar { background: #26282c; border: none; spacing: 6px; padding: 4px; }
"""


def stylesheet_for(theme_name: str) -> str:
    return DARK_QSS if theme_name == "dark" else LIGHT_QSS
