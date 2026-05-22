"""Google APIの初回認証を行うスクリプト。一度だけ実行してください。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.gmail_service import _get_creds

if __name__ == "__main__":
    creds = _get_creds()
    print(f"認証完了: {creds.token[:20]}...")
    print(f"トークン保存先: {os.path.abspath('credentials/google_token.json')}")
