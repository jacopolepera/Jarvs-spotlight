
BASE_SCROLLBAR_CSS = """
QScrollBar:vertical { border: none; background: transparent; width: 6px; margin: 0px; }
QScrollBar::handle:vertical { background: rgba(120, 120, 120, 150); border-radius: 3px; min-height: 20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""

# Base advanced stylesheet for Markdown
MARKDOWN_BASE_CSS = """
h1, h2, h3 { font-size: 13px; font-weight: bold; margin-top: 4px; margin-bottom: 4px; }
p { margin-top: 2px; margin-bottom: 2px; line-height: 1.3; }
pre { padding: 10px; border-radius: 6px; font-family: Consolas, monospace; margin: 8px 0px; }
code { font-family: Consolas, monospace; padding: 2px 4px; border-radius: 4px; }
a { text-decoration: none; }
"""

def get_theme_data(theme_name: str):
    """
    Returns a tuple: (Main_App_QSS, Markdown_CSS)
    """
    if theme_name == "Light":
        main_qss = f"""
            QWidget#GradientWrapper {{ background-color: #cccccc; border-radius: 10px; }}
            QWidget#MainContainer {{ background-color: #ffffff; border-radius: 9px; }}
            QToolTip {{ color: #000; background-color: #fafafa; border: 1px solid #ccc; border-radius: 4px; padding: 4px; font-size: 11px; }}
            QLabel#TitleLabel {{ color: #555; font-weight: bold; font-size: 11px; padding: 0px 5px; }}
            QPushButton#SettingsBtn {{ background-color: transparent; color: #555; border: none; font-size: 14px; }}
            QPushButton#SettingsBtn:hover {{ color: #000; background-color: rgba(0,0,0,0.08); border-radius: 5px; }}
            QPushButton#MinBtn {{ background-color: transparent; color: #555; border: none; font-size: 16px; }}
            QPushButton#MinBtn:hover {{ color: #000; background-color: rgba(0,0,0,0.08); border-radius: 5px; }}
            QPushButton#CloseBtn {{ background-color: transparent; color: #555; border: none; font-size: 12px; }}
            QPushButton#CloseBtn:hover {{ color: white; background-color: rgba(255,85,85,0.8); border-radius: 5px; }}
            QTextEdit#InputBox {{ background-color: #f1f1f1; color: #111; border-radius: 6px; padding: 8px 10px; border: 1px solid #ddd; }}
            QPushButton#AttachBtn {{ background-color: rgba(0,0,0,0.05); color: #777; border: 1px solid #ccc; border-radius: 14px; font-size: 16px; }}
            QPushButton#AttachBtn:hover {{ color: #333; background-color: rgba(0,0,0,0.1); border-color: #aaa; }}
            QPushButton#MicBtn {{ background-color: rgba(0,0,0,0.05); color: #777; border: 1px solid #ccc; border-radius: 14px; font-size: 16px; }}
            QPushButton#MicBtn:hover {{ color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: #ff5555; }}
            QTextEdit#OutputBox {{ background-color: transparent; color: #222; border: none; padding: 0px; }}
            {BASE_SCROLLBAR_CSS}
        """
        md_qss = MARKDOWN_BASE_CSS + "h1, h2, h3 { color: #111; } pre { background-color: #eee; color: #222; border: 1px solid #ddd; } code { background-color: #f5f5f5; color: #d03a3a; } a { color: #0066cc; }"

    elif theme_name == "Dark":
        main_qss = f"""
            QWidget#GradientWrapper {{ background-color: #333333; border-radius: 10px; }}
            QWidget#MainContainer {{ background-color: #1a1a1a; border-radius: 9px; }}
            QToolTip {{ color: #eee; background-color: #222; border: 1px solid #444; border-radius: 4px; padding: 4px; font-size: 11px; }}
            QLabel#TitleLabel {{ color: #888; font-weight: bold; font-size: 11px; padding: 0px 5px; }}
            QPushButton#SettingsBtn {{ background-color: transparent; color: #888; border: none; font-size: 14px; }}
            QPushButton#SettingsBtn:hover {{ color: white; background-color: rgba(255,255,255,0.08); border-radius: 5px; }}
            QPushButton#MinBtn {{ background-color: transparent; color: #888; border: none; font-size: 16px; }}
            QPushButton#MinBtn:hover {{ color: white; background-color: rgba(255,255,255,0.08); border-radius: 5px; }}
            QPushButton#CloseBtn {{ background-color: transparent; color: #888; border: none; font-size: 12px; }}
            QPushButton#CloseBtn:hover {{ color: white; background-color: rgba(255,85,85,0.3); border-radius: 5px; }}
            QTextEdit#InputBox {{ background-color: #262626; color: white; border-radius: 6px; padding: 8px 10px; border: 1px solid #333; }}
            QPushButton#AttachBtn {{ background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; }}
            QPushButton#AttachBtn:hover {{ color: #ddd; background-color: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.3); }}
            QPushButton#MicBtn {{ background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; }}
            QPushButton#MicBtn:hover {{ color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: rgba(255,85,85,0.4); }}
            QTextEdit#OutputBox {{ background-color: transparent; color: #ddd; border: none; padding: 0px; }}
            {BASE_SCROLLBAR_CSS}
        """
        md_qss = MARKDOWN_BASE_CSS + "h1, h2, h3 { color: #eee; } pre { background-color: #111; color: #e0e0e0; border: 1px solid #222; } code { background-color: #2b2b2b; color: #d4d4d4; } a { color: #58a6ff; }"

    elif theme_name == "Glassy Windows":
        main_qss = f"""
            QWidget#GradientWrapper {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(50, 60, 80, 175),
                    stop:0.5 rgba(38, 44, 58, 155),
                    stop:1 rgba(45, 52, 70, 175));
                border-radius: 10px;
            }}
            QWidget#MainContainer {{
                background-color: rgba(20, 23, 30, 145);
                border-radius: 9px;
            }}
            QToolTip {{ color: #dde; background-color: rgba(30, 34, 46, 230); border: 1px solid rgba(120,140,200,0.4); border-radius: 4px; padding: 4px; font-size: 11px; }}
            QLabel#TitleLabel {{ color: rgba(200, 210, 230, 0.85); font-weight: bold; font-size: 11px; padding: 0px 5px; }}
            QPushButton#SettingsBtn {{ background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 14px; }}
            QPushButton#SettingsBtn:hover {{ color: white; background-color: rgba(255,255,255,0.12); border-radius: 5px; }}
            QPushButton#MinBtn {{ background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 16px; }}
            QPushButton#MinBtn:hover {{ color: white; background-color: rgba(255,255,255,0.12); border-radius: 5px; }}
            QPushButton#CloseBtn {{ background-color: transparent; color: rgba(200,210,230,0.8); border: none; font-size: 12px; }}
            QPushButton#CloseBtn:hover {{ color: white; background-color: rgba(255,85,85,0.45); border-radius: 5px; }}
            QPushButton#AttachBtn {{ background-color: transparent; color: rgba(200,210,230,0.8); border: 1px solid rgba(120,140,200,0.3); border-radius: 14px; font-size: 16px; }}
            QPushButton#AttachBtn:hover {{ color: white; background-color: rgba(255,255,255,0.12); border-radius: 14px; }}
            QPushButton#MicBtn {{ background-color: transparent; color: rgba(200,210,230,0.8); border: 1px solid rgba(120,140,200,0.3); border-radius: 14px; font-size: 16px; }}
            QPushButton#MicBtn:hover {{ color: #ff8888; background-color: rgba(255,85,85,0.15); border-color: rgba(255,85,85,0.4); }}
            QTextEdit#InputBox {{ background-color: rgba(255, 255, 255, 0.07); color: rgba(220,228,245,1); border-radius: 6px; padding: 8px 10px; border: 1px solid rgba(120,140,200,0.3); }}
            QTextEdit#OutputBox {{ background-color: transparent; color: rgba(220,228,245,1); border: none; padding: 0px; }}
            {BASE_SCROLLBAR_CSS}
        """
        md_qss = MARKDOWN_BASE_CSS + "h1, h2, h3 { color: #ccd6f6; } pre { background-color: rgba(10,14,26,0.55); color: #ccd6f6; border: 1px solid rgba(100,130,200,0.25); } code { background-color: rgba(255,255,255,0.1); color: #a8d8ff; } a { color: #7eb8ff; }"

    else:
        # AI Vaporwave (Default)
        main_qss = f"""
            QWidget#GradientWrapper {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 rgba(80, 160, 255, 180), 
                    stop:0.5 rgba(150, 80, 255, 180), 
                    stop:1 rgba(255, 100, 140, 180));
                border-radius: 10px;
            }}
            QWidget#MainContainer {{
                background-color: rgba(18, 18, 18, 230);
                border-radius: 9px;
            }}
            QToolTip {{ color: #eee; background-color: #222; border: 1px solid #444; border-radius: 4px; padding: 4px; font-size: 11px; }}
            QLabel#TitleLabel {{ color: #888; font-weight: bold; font-size: 11px; padding: 0px 5px; }}
            QPushButton#SettingsBtn {{ background-color: transparent; color: #888; border: none; font-size: 14px; }}
            QPushButton#SettingsBtn:hover {{ color: white; background-color: rgba(255,255,255,0.1); border-radius: 5px; }}
            QPushButton#MinBtn {{ background-color: transparent; color: #888; border: none; font-size: 16px; }}
            QPushButton#MinBtn:hover {{ color: white; background-color: rgba(255,255,255,0.1); border-radius: 5px; }}
            QPushButton#CloseBtn {{ background-color: transparent; color: #888; border: none; font-size: 12px; }}
            QPushButton#CloseBtn:hover {{ color: white; background-color: rgba(255,85,85,0.4); border-radius: 5px; }}
            QPushButton#AttachBtn {{ background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; }}
            QPushButton#AttachBtn:hover {{ color: #ddd; background-color: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.3); }}
            QPushButton#MicBtn {{ background-color: transparent; color: #888; border: 1px solid rgba(255,255,255,0.15); border-radius: 14px; font-size: 16px; }}
            QPushButton#MicBtn:hover {{ color: #ff5555; background-color: rgba(255,85,85,0.1); border-color: rgba(255,85,85,0.4); }}
            QTextEdit#InputBox {{ background-color: rgba(30, 30, 30, 200); color: white; border-radius: 6px; padding: 8px 10px; border: 1px solid rgba(80, 80, 80, 100); }}
            QTextEdit#OutputBox {{ background-color: transparent; color: #D4D4D4; border: none; padding: 0px; }}
            {BASE_SCROLLBAR_CSS}
        """
        md_qss = MARKDOWN_BASE_CSS + "h1, h2, h3 { color: #eee; } pre { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #333; } code { background-color: #2b2b2b; color: #d4d4d4; } a { color: #58a6ff; }"

    return main_qss, md_qss
