#!/usr/bin/env python3
"""
ローカル results.db の問題を Railway 上の gradify バックエンドに直接 POST するスクリプト。
使い方: python3 push_to_railway.py https://your-app.railway.app
"""
import json, os, sys, sqlite3, time
import urllib.request

LOCAL_DB = '/home/ubu/gradify_backend/data/results.db'
DECK_NAME = '乳腺専門医試験'

if len(sys.argv) < 2:
    print('使い方: python3 push_to_railway.py https://your-app.railway.app')
    sys.exit(1)

BASE_URL = sys.argv[1].rstrip('/')
ADMIN_TOKEN = os.environ.get('GRADIFY_ADMIN_TOKEN', '')

def req(method, path, body=None):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body else None
    headers = {'Content-Type': 'application/json'}
    if ADMIN_TOKEN:
        headers['X-Admin-Token'] = ADMIN_TOKEN
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())

def main():
    # 1. ローカルDBから問題取得
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    local_qs = conn.execute('SELECT * FROM questions').fetchall()
    conn.close()
    print(f'ローカルDB: {len(local_qs)}問')

    # 2. デッキ確認／作成
    decks = req('GET', '/api/decks')
    deck = next((d for d in decks if d['name'] == DECK_NAME), None)
    if deck:
        deck_id = deck['id']
        print(f'既存デッキ使用: id={deck_id}')
    else:
        deck_id = req('POST', '/api/decks',
            {'name': DECK_NAME, 'description': '乳腺専門医試験対策', 'category': '乳腺外科'})['id']
        print(f'デッキ作成: id={deck_id}')

    # 3. 既存問題テキストを取得（重複スキップ）
    existing_qs = req('GET', f'/api/decks/{deck_id}/questions')
    existing = {q['question'] for q in existing_qs}
    print(f'Railway既存: {len(existing)}問')

    # 4. 投入
    ok = skip = err = 0
    for q in local_qs:
        text = (q['question'] or '').strip()
        if not text or text in existing:
            skip += 1
            continue
        try:
            payload = {
                'question':      text,
                'model_answer':  q['model_answer'] or '',
                'key_points':    q['key_points'] or '[]',
                'category':      q['category'] or '',
                'guideline_ref': q['guideline_ref'] or '',
                'flowchart':     q['flowchart'] or '',
            }
            if q['mcq_options']:
                payload['mcq_options'] = json.loads(q['mcq_options'])
            req('POST', f'/api/decks/{deck_id}/questions', payload)
            existing.add(text)
            ok += 1
        except Exception as e:
            err += 1
            print(f'  エラー: {e}')
        if (ok + err) % 50 == 0 and ok + err > 0:
            print(f'  {ok}問 投入済み...')
        time.sleep(0.05)

    print(f'\n完了: 追加={ok} / スキップ={skip} / エラー={err}')

if __name__ == '__main__':
    main()
