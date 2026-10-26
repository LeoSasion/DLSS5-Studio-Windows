import os
os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
from app import demo
from ui_style import CSS, THEME

if __name__ == '__main__':
    demo.queue().launch(server_name='127.0.0.1', server_port=7860, inbrowser=False, theme=THEME, css=CSS)
