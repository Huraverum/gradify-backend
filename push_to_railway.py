#!/usr/bin/env python3
"""
NAS 8689 の問題を Railway 上の gradify バックエンドに直接 POST するスクリプト。
使い方: python3 push_to_railway.py https://your-app.railway.app
"""
import json, sys, ssl, time
import urllib.request, urllib.error

if len(sys.argv) < 2:
    print('使い方: python3 push_to_railway.py https://your-app.railway.app')
    sys.exit(1)

BASE_URL  = sys.argv[1].rstrip('/')
SRC_URL   = 'https://192.168.1.50:8689/api/exam-questions'
DECK_NAME = '乳腺専門医試験'

def req(method, path, body=None):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())

def main():
    # 1. 問題取得 (NAS)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    print('NASから問題を取得中...', flush=True)
    with urllib.request.urlopen(SRC_URL, context=ctx) as r:
        questions = json.loads(r.read())
    print(f'{len(questions)}問 取得', flush=True)

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
    print(f'既存: {len(existing)}問')

    # 4. 投入
    ok = skip = err = 0
    for i, q in enumerate(questions):
        text = (q.get('question') or '').strip()
        if not text or text in existing:
            skip += 1
            continue
        kps = q.get('key_points') or []
        model = '\n'.join(f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
        try:
            req('POST', f'/api/decks/{deck_id}/questions', {
                'question':     text,
                'model_answer': model or '（模範解答未設定）',
                'key_points':   kps,
                'category':     (q.get('category') or '').strip(),
                'guideline_ref': q.get('guideline_ref') or '',
                'flowchart':    q.get('flowchart') or '',
            })
            existing.add(text)
            ok += 1
        except Exception as e:
            err += 1
            print(f'  エラー [{i}]: {e}', flush=True)
        if (ok + err) % 50 == 0 and ok + err > 0:
            print(f'  {ok}問 投入済み...', flush=True)
        time.sleep(0.05)  # レート制限回避

    print(f'\n完了: 追加={ok} / スキップ={skip} / エラー={err}')

if __name__ == '__main__':
    main()
