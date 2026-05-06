#!/usr/bin/env python3
"""
NAS port 8689 の exam-questions を gradify DB に投入するスクリプト。
使い方: python3 seed_from_8689.py
"""
import json, sqlite3, urllib.request, ssl, sys, os

SRC_URL   = 'https://192.168.1.50:8689/api/exam-questions'
DECK_NAME = '乳腺専門医試験'
DB_PATH   = os.path.join(os.path.dirname(__file__), 'data', 'results.db')

def main():
    # 1. 問題取得
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    print(f'Fetching from {SRC_URL} ...', flush=True)
    with urllib.request.urlopen(SRC_URL, context=ctx) as r:
        questions = json.loads(r.read())
    print(f'{len(questions)}問 取得完了', flush=True)

    # 2. DB 接続
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')

    # 3. デッキ作成（既存なら使う）
    row = conn.execute('SELECT id FROM decks WHERE name=?', (DECK_NAME,)).fetchone()
    if row:
        deck_id = row[0]
        print(f'既存デッキ使用: id={deck_id}')
    else:
        cur = conn.execute(
            'INSERT INTO decks(name,description,category,created_at) VALUES(?,?,?,datetime("now"))',
            (DECK_NAME, '乳腺専門医試験対策', '乳腺外科')
        )
        deck_id = cur.lastrowid
        print(f'デッキ作成: id={deck_id}')

    # 4. 既存問題のテキスト一覧（重複スキップ用）
    existing = set(
        r[0] for r in conn.execute('SELECT question FROM questions WHERE deck_id=?', (deck_id,))
    )
    print(f'既存問題数: {len(existing)}')

    # 5. 投入
    inserted = 0
    for q in questions:
        text = (q.get('question') or '').strip()
        if not text or text in existing:
            continue
        kps = q.get('key_points') or []
        # model_answer は 8689 には無いので key_points から生成
        model = '\n'.join(f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
        conn.execute(
            'INSERT INTO questions(deck_id,category,question,model_answer,key_points,guideline_ref,flowchart,created_at) VALUES(?,?,?,?,?,?,?,datetime("now"))',
            (deck_id,
             (q.get('category') or '').strip(),
             text,
             model or '（模範解答未設定）',
             json.dumps(kps, ensure_ascii=False),
             q.get('guideline_ref') or '',
             q.get('flowchart') or '',
            )
        )
        existing.add(text)
        inserted += 1
        if inserted % 50 == 0:
            print(f'  {inserted}問 投入中...', flush=True)

    conn.commit()
    conn.close()
    print(f'完了: {inserted}問 を deck_id={deck_id} に追加しました')

if __name__ == '__main__':
    main()
