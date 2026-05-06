#!/usr/bin/env python3
"""
ローカルDBの全問題に対してMCQ選択肢を一括生成してキャッシュするスクリプト。
使い方: python3 generate_mcq_cache.py sk-ant-...
生成後は push_to_railway.py でキャッシュごとRailwayに送れる。
"""
import json, re, sys, sqlite3, time, random

LOCAL_DB = '/home/ubu/gradify_backend/data/results.db'

if len(sys.argv) < 2:
    print('使い方: python3 generate_mcq_cache.py <Anthropic APIキー>')
    sys.exit(1)

api_key = sys.argv[1]

import anthropic
client = anthropic.Anthropic(api_key=api_key)

conn = sqlite3.connect(LOCAL_DB)
conn.row_factory = sqlite3.Row

# mcq_options カラムがなければ追加
cols = [r[1] for r in conn.execute('PRAGMA table_info(questions)').fetchall()]
if 'mcq_options' not in cols:
    conn.execute('ALTER TABLE questions ADD COLUMN mcq_options TEXT DEFAULT NULL')
    conn.commit()

rows = conn.execute(
    'SELECT id, question, model_answer, key_points FROM questions WHERE mcq_options IS NULL'
).fetchall()
total = len(rows)
print(f'未キャッシュ: {total}問')
if total == 0:
    print('すべてキャッシュ済みです。')
    conn.close(); sys.exit(0)

ok = err = 0
for i, row in enumerate(rows, 1):
    kps = json.loads(row['key_points'] or '[]')
    answer_text = row['model_answer'] or '\n'.join(
        f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
    prompt = f"""以下の記述式問題と正解をもとに、5択選択肢を日本語で作成してください。

問題: {row['question']}

正解の要点:
{answer_text[:600]}

要件:
- 選択肢は5つ（正解1つ＋紛らわしい誤答4つ）
- 正解は模範解答の核心を1〜2文で簡潔にまとめる
- 誤答はそれぞれ異なる方向の誤り（過剰・不足・混同・逆・部分正解など）
- 各選択肢は30〜60字程度

必ずこのJSONのみを返してください（前後にテキスト不要）:
{{"options":["正解テキスト","誤答1","誤答2","誤答3","誤答4"],"correct_index":0}}"""
    try:
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}])
        raw = msg.content[0].text.strip()
        data = json.loads(re.search(r'\{.*\}', raw, re.S).group())
        options = data['options']
        correct = options[data['correct_index']]
        random.shuffle(options)
        result = {'options': options, 'correct_index': options.index(correct)}
        conn.execute('UPDATE questions SET mcq_options=? WHERE id=?',
                     (json.dumps(result, ensure_ascii=False), row['id']))
        conn.commit()
        ok += 1
        print(f'  [{i}/{total}] OK: {row["question"][:40]}…')
    except Exception as e:
        err += 1
        print(f'  [{i}/{total}] ERR: {e}')
    time.sleep(0.3)

conn.close()
print(f'\n完了: 生成={ok} / エラー={err}')
print('次のステップ: python3 push_to_railway.py <Railway URL> でキャッシュごとプッシュ')
