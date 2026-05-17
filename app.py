import os, json, re, sqlite3, base64
from datetime import datetime
from io import BytesIO
from html.parser import HTMLParser
from flask import Flask, request, Response, stream_with_context, jsonify
import anthropic

app = Flask(__name__)

PORT      = int(os.environ.get('PORT', 8000))
DATA_DIR  = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
RESULTS_DB = os.path.join(DATA_DIR, 'results.db')
os.makedirs(DATA_DIR, exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── CORS ──────────────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    r.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return r

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options(_path=''):
    return jsonify({}), 200

# ── DB ────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(RESULTS_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn

# ── Rate limit (Phase 1: per-IP daily quota) ──────────────────
RATE_LIMITS = {
    'score':    int(os.environ.get('RATE_LIMIT_SCORE', 30)),
    'ai':       int(os.environ.get('RATE_LIMIT_AI',    50)),
    'extract':  int(os.environ.get('RATE_LIMIT_EXTRACT', 5)),
}

def _client_id():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def _check_rate(bucket):
    limit = RATE_LIMITS.get(bucket, 0)
    if limit <= 0:
        return True, 0, 0
    day = datetime.now().strftime('%Y-%m-%d')
    cid = _client_id()
    conn = get_db()
    row = conn.execute(
        'SELECT count FROM rate_limits WHERE client_id=? AND bucket=? AND day=?',
        (cid, bucket, day)
    ).fetchone()
    current = row['count'] if row else 0
    if current >= limit:
        conn.close()
        return False, current, limit
    conn.execute(
        'INSERT INTO rate_limits (client_id, bucket, day, count) VALUES (?, ?, ?, 1) '
        'ON CONFLICT(client_id, bucket, day) DO UPDATE SET count = count + 1',
        (cid, bucket, day)
    )
    conn.commit()
    conn.close()
    return True, current + 1, limit

def _rate_limit_response(bucket, current, limit):
    return jsonify({
        'error': '本日の利用上限に達しました',
        'detail': f'1日{limit}回まで（明日0時にリセット）',
        'bucket': bucket,
        'current': current,
        'limit': limit,
    }), 429

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id INTEGER,
            category TEXT NOT NULL,
            question TEXT NOT NULL,
            model_answer TEXT,
            key_points TEXT,
            guideline_ref TEXT,
            flowchart TEXT,
            created_at TEXT,
            FOREIGN KEY(deck_id) REFERENCES decks(id)
        );
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER UNIQUE,
            score INTEGER,
            grade TEXT,
            answer TEXT,
            model_answer TEXT,
            covered TEXT,
            partial TEXT,
            missed TEXT,
            feedback TEXT,
            advice TEXT,
            saved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER,
            score INTEGER,
            grade TEXT,
            answer TEXT,
            model_answer TEXT,
            covered TEXT,
            partial TEXT,
            missed TEXT,
            feedback TEXT,
            advice TEXT,
            attempted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ghost_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            square INTEGER NOT NULL,
            message TEXT NOT NULL,
            author TEXT,
            likes INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS wallet (
            id INTEGER PRIMARY KEY CHECK(id=1),
            balance INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS achievement_unlocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            achievement_id TEXT NOT NULL,
            user_token TEXT NOT NULL,
            unlocked_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(achievement_id, user_token)
        );
    ''')
    conn.execute('INSERT OR IGNORE INTO wallet(id, balance) VALUES(1, 0)')
    # migration: add mcq_options column if missing
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(questions)').fetchall()]
    if 'mcq_options' not in cols:
        conn.execute('ALTER TABLE questions ADD COLUMN mcq_options TEXT DEFAULT NULL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            client_id TEXT NOT NULL,
            bucket TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (client_id, bucket, day)
        )
    ''')
    conn.commit()
    conn.close()

GHOST_SEEDS = [
    (1,  "ここから全てが始まった。震えていたことを今でも覚えている。", "名もなき先輩"),
    (3,  "三歩進んで二歩下がる。それでも前に進め。", "浪人生"),
    (5,  "最初の五問。意外と手が動いた。", "合格者"),
    (8,  "もう八マス。諦めなければ必ず進める。", "通りすがりの受験生"),
    (10, "第一関門を越えた者へ。この先はもっと険しい。だが、越えられる。", "先人"),
    (13, "十三という数字が不吉だと思っていた。でも何も起きなかった。", "文系浪人"),
    (15, "折り返しの半分。まだ半分。どちらで見るかで全てが変わる。", "哲学科の学生"),
    (20, "二つ目の関門を越えた。ここから先は頂上だけを見ろ。", "再挑戦中の旅人"),
    (25, "あと五マス。合格の二文字が見えるか？", "元受験生"),
    (30, "おめでとう。ここまで来た者だけが知っている景色がある。", "ゴールの向こうから"),
    (33, "人生の三分の一に似た数字。まだ何でもできる。", "社会人受験生"),
    (40, "十分の四。誰かが「ここが本当の始まり」と言っていた。", "予備校講師"),
    (42, "宇宙の答えは42だと聞いた。あなたの答えは何マス先にある？", "理系の誰か"),
    (50, "半分。ここで泣いた。悔しくて。嬉しくて。両方だった。", "去年の合格者"),
    (55, "過去問を初めて解いた夜を思い出す。何も解けなかった。", "司法試験合格者"),
    (60, "六十マス。見えてきた気がする。その感覚を信じろ。", "公認会計士"),
    (66, "六六六。不吉な数字だと笑ってくれ。あなたはここを越えた。", "受験オタク"),
    (70, "七十マス。正直、ここまで来ると思っていなかった。", "自分への手紙"),
    (75, "四分の三。残りが見えてきた。でも油断するな。最後が一番きつい。", "宅建合格者"),
    (80, "八十マス。あと二十。もう止まれないところまで来た。", "TOEIC満点者"),
    (88, "八十八夜。お茶ではなく、合格を摘み取れ。", "農学部の誰か"),
    (90, "九十マス。ここまで来た自分を褒めてやれ。本当に。", "頂上を制した者"),
    (95, "あと五マス。合格の二文字が見えるか？", "司法書士"),
    (99, "あと一歩。震えているか？それでいい。", "ゴール直前の誰か"),
    (100,"おめでとう。ここまで来た者だけが知っている景色がある。", "ゴールの向こうから"),
]

def migrate_db():
    conn = get_db()
    try:
        conn.execute('ALTER TABLE questions ADD COLUMN model_answer TEXT')
        conn.commit()
    except Exception:
        pass
    # 重複ghost_messagesを削除（同じsquare+messageの最古IDのみ残す）
    conn.execute('''
        DELETE FROM ghost_messages WHERE id NOT IN (
            SELECT MIN(id) FROM ghost_messages GROUP BY square, message
        )
    ''')
    # 一掃: _fallback_mcq_options で書き込まれた壊れたMCQキャッシュを削除
    conn.execute(
        "UPDATE questions SET mcq_options=NULL "
        "WHERE mcq_options LIKE '%この問題では、主要な要点ではなく%'"
    )
    conn.commit()
    # seed ghost messages if empty
    count = conn.execute('SELECT COUNT(*) FROM ghost_messages').fetchone()[0]
    if count == 0:
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        conn.executemany(
            'INSERT INTO ghost_messages(square,message,author,created_at) VALUES(?,?,?,?)',
            [(sq, msg, author, now) for sq, msg, author in GHOST_SEEDS])
        conn.commit()
    conn.close()

init_db()
migrate_db()

COIN_MAP = {'S': 50, 'A': 40, 'B': 30, 'C': 20, 'D': 10}

# ── Scoring ───────────────────────────────────────────────────
SCORE_SYSTEM = """あなたはAI採点官です。受験者の回答を加重採点し、模範解答も作成してください。必ず以下のJSON形式のみで返答してください。前後に余分なテキストや```json```などを含めないこと。

【採点手順】
1. 「採点キーポイント」に列挙された各項目を一つずつ確認する。
2. 各キーポイントについて、受験者の回答を以下の基準で判定する：
   - covered（100%）: キーポイントの核心的な内容・キーワードが明確に述べられている
   - partial（70%）: キーポイントに関連する内容・概念・方向性に少しでも触れている（詳細・数値・語句が不十分でも可）
   - missed（0%）: 完全に無言及、または明らかに的外れ・誤った内容
3. 判定に基づき、そのキーポイントの文言をそのまま covered/partial/missed のいずれかに振り分ける。
4. スコア計算：重み w=3→15点満点, w=2→10点満点, w=1→5点満点。covered=100%, partial=70%, missed=0%。各キーポイントのスコアを合算し、総満点で割って100点換算する。

【判定ルール】
- 迷った場合は必ず partial を選ぶ。missed は「完全に無言及」または「明らかに誤り」の場合のみ
- キーワードが正確なら covered、少しでも関連していれば partial、全く無関係・誤りのみ missed
- 【音声入力救済】回答は音声認識による誤字を含む場合がある。同音異義語・類似音の置換（例:漢数字↔アラビア数字、専門用語の音写ミス）が疑われるときは意味が通じれば covered として扱う
- covered/partial/missed のどれかに必ず全キーポイントを分類すること
- covered/partial/missed には必ずキーポイントの実際の文言を入れること（「なし」「該当なし」は不可）

{"score":整数0-100,"grade":"S/A/B/C/D","covered":["完全に網羅できたキーポイントの文言"],"partial":["部分的にしか触れていないキーポイントの文言"],"missed":["全く触れていない・誤ったキーポイントの文言"],"feedback":"総合フィードバック（3文）","advice":"今後の学習アドバイス（1-2文）","model_answer":"模範解答（400字程度）"}

採点基準：S=90-100点、A=75-89点、B=60-74点、C=40-59点、D=0-39点"""

EXTRACT_PROMPT = """問題・設問を抽出してください。以下のJSON配列形式のみで返してください（前後の説明・コードブロック不要）:
[{"category":"カテゴリ名","question":"記述式の問題文","model_answer":"模範解答（200字程度）","key_points":[{"t":"採点ポイント","w":重み整数(3=必須/2=重要/1=加点)}],"guideline_ref":"参照（なければ空文字）","flowchart":"思考フロー→区切り（なければ空文字）"}]
ルール: 選択問題は記述式に変換。key_pointsは3〜8個。問題がなければ[]。"""

VISION_QUIZ_PROMPT = """画像に写っている対象を観察し、それを教材として学習用クイズを3問生成してください。

判定ルール:
1. まず画像の主題を特定する（例: 植物・昆虫・空/雲・建物・地形・食べ物・美術作品・看板/文字・その他）
2. その対象に関する事実・分類・名称・特徴・歴史・科学的背景などから、自然な好奇心を刺激する設問を3問作る
3. 設問は記述式（短答）。難易度はA=やさしい、B=ふつう、C=難しい を1問ずつ
4. 学習者は10〜大人を想定。専門用語は使ってもよいが必ず短い解説を添える

出力は以下のJSONのみ（前後の説明・コードブロック不要）:
{"subject":"何が写っているかの一語","domain":"植物/昆虫/空/建物/地形/食べ物/美術/看板/その他","summary":"対象の60字以内の解説","questions":[{"q":"問題文","a":"模範解答（80字以内）","level":"A/B/C","key_points":[{"t":"採点ポイント","w":1〜3}]}]}

画像が不鮮明・対象不明・不適切な内容の場合は {"subject":"","domain":"","summary":"判別不能","questions":[]} を返す。"""

# ── Deck API ──────────────────────────────────────────────────
@app.route('/api/decks', methods=['GET'])
def get_decks():
    conn = get_db()
    rows = conn.execute('SELECT d.*, COUNT(q.id) as q_count FROM decks d LEFT JOIN questions q ON d.id=q.deck_id GROUP BY d.id ORDER BY d.created_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/decks', methods=['POST'])
def create_deck():
    d = request.get_json(force=True)
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'デッキ名は必須です'}), 400
    conn = get_db()
    cur = conn.execute('INSERT INTO decks(name,description,category,created_at) VALUES(?,?,?,?)',
        (name, d.get('description',''), d.get('category',''), datetime.now().strftime('%Y-%m-%d %H:%M')))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/decks/<int:deck_id>', methods=['DELETE'])
def delete_deck(deck_id):
    conn = get_db()
    conn.execute('DELETE FROM questions WHERE deck_id=?', (deck_id,))
    conn.execute('DELETE FROM decks WHERE id=?', (deck_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Question API ──────────────────────────────────────────────
@app.route('/api/decks/<int:deck_id>/questions', methods=['GET'])
def get_questions(deck_id):
    category = request.args.get('category', '')
    limit    = request.args.get('limit', type=int)
    shuffle  = request.args.get('shuffle', '0') == '1'
    mode     = request.args.get('mode', '')   # 'weak' | 'new' | ''
    conn = get_db()

    cat_clause  = ' AND q.category=?' if category else ''
    cat_params  = [category]              if category else []

    if mode == 'weak':
        # Attempted questions, lowest avg score first
        sql  = f'''SELECT q.* FROM questions q
                   JOIN attempts a ON a.question_id = q.id
                   WHERE q.deck_id=? {cat_clause}
                   GROUP BY q.id
                   ORDER BY AVG(a.score) ASC'''
        rows = conn.execute(sql, [deck_id] + cat_params).fetchall()
    elif mode == 'new':
        # Questions with zero attempts
        sql  = f'''SELECT q.* FROM questions q
                   WHERE q.deck_id=?
                   AND q.id NOT IN (SELECT DISTINCT question_id FROM attempts)
                   {cat_clause}'''
        rows = conn.execute(sql, [deck_id] + cat_params).fetchall()
        if shuffle:
            import random; random.shuffle(rows := list(rows))
    else:
        cat_sql = ' AND category=?' if category else ''
        order   = ' ORDER BY RANDOM()' if shuffle else ' ORDER BY id ASC'
        sql  = f'SELECT * FROM questions WHERE deck_id=? {cat_sql}{order}'
        rows = conn.execute(sql, [deck_id] + cat_params).fetchall()

    conn.close()
    result = []
    for r in rows:
        q = dict(r)
        q['key_points'] = json.loads(q['key_points'] or '[]')
        result.append(q)
    if limit:
        result = result[:limit]
    return jsonify(result)

@app.route('/api/decks/<int:deck_id>/questions', methods=['POST'])
def add_question(deck_id):
    d = request.get_json(force=True)
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': '問題文は必須です'}), 400
    conn = get_db()
    mcq_options = d.get('mcq_options')  # pre-cached options from push script
    cur = conn.execute(
        'INSERT INTO questions(deck_id,category,question,model_answer,key_points,guideline_ref,flowchart,mcq_options,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (deck_id, (d.get('category') or '').strip(), question,
         d.get('model_answer', ''),
         json.dumps(d.get('key_points', []), ensure_ascii=False),
         d.get('guideline_ref', ''), d.get('flowchart', ''),
         json.dumps(mcq_options) if mcq_options else None,
         datetime.now().strftime('%Y-%m-%d %H:%M')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': cur.lastrowid})

@app.route('/api/decks/<int:deck_id>/categories')
def get_categories(deck_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT category FROM questions WHERE deck_id=? AND category IS NOT NULL AND category!='' ORDER BY category",
        (deck_id,)).fetchall()
    conn.close()
    return jsonify([r['category'] for r in rows])

@app.route('/api/questions/<int:q_id>', methods=['PUT'])
def update_question(q_id):
    d = request.get_json(force=True)
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': '問題文は必須です'}), 400
    conn = get_db()
    conn.execute(
        'UPDATE questions SET category=?,question=?,model_answer=?,key_points=?,guideline_ref=?,flowchart=? WHERE id=?',
        ((d.get('category') or '').strip(), question,
         d.get('model_answer', ''),
         json.dumps(d.get('key_points', []), ensure_ascii=False),
         d.get('guideline_ref', ''), d.get('flowchart', ''), q_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/questions/<int:q_id>', methods=['DELETE'])
def delete_question(q_id):
    conn = get_db()
    conn.execute('DELETE FROM questions WHERE id=?', (q_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Scoring API ───────────────────────────────────────────────
@app.route('/api/score', methods=['POST'])
def score():
    d = request.get_json(force=True)
    qid    = d.get('question_id')
    answer = (d.get('answer') or '').strip()
    user_key = ANTHROPIC_API_KEY
    if not answer:
        return jsonify({'error': '回答を入力してください'}), 400
    if not user_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('score')
    if not ok:
        return _rate_limit_response('score', current, limit)

    conn = get_db()
    row = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '問題が見つかりません'}), 404

    kps = json.loads(row['key_points'] or '[]')
    kp_str = '\n'.join(f"- (w={k['w']}) {k['t']}" for k in kps)

    def generate():
        client = anthropic.Anthropic(api_key=user_key)
        full = ''
        with client.messages.stream(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            temperature=0,
            system=SCORE_SYSTEM,
            messages=[{'role':'user','content':
                f"【問題】{row['question']}\n\n【採点キーポイント】\n{kp_str}\n\n【受験者の回答】\n{answer}"}]
        ) as s:
            for t in s.text_stream:
                full += t
                yield f"data: {json.dumps({'chunk': t}, ensure_ascii=False)}\n\n"
        try:
            data = json.loads(full)
            _save_attempt(qid, data, answer)
            yield f"data: {json.dumps({'done': True, **data}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/score-sync', methods=['POST'])
def score_sync():
    d = request.get_json(force=True)
    qid      = d.get('question_id')
    answer   = (d.get('answer') or '').strip()
    user_key = ANTHROPIC_API_KEY
    companion = d.get('companion')
    if not answer:
        return jsonify({'error': '回答を入力してください'}), 400
    if not user_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('score')
    if not ok:
        return _rate_limit_response('score', current, limit)

    conn = get_db()
    row = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '問題が見つかりません'}), 404

    kps = json.loads(row['key_points'] or '[]')
    kp_str = '\n'.join(f"- (w={k['w']}) {k['t']}" for k in kps)

    client = anthropic.Anthropic(api_key=user_key)
    msg = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=2000,
        temperature=0,
        system=SCORE_SYSTEM,
        messages=[{'role':'user','content':
            f"【問題】{row['question']}\n\n【採点キーポイント】\n{kp_str}\n\n【受験者の回答】\n{answer}"}]
    )
    text = msg.content[0].text.strip()
    try:
        result = json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(m.group()) if m else {}
    if not result:
        return jsonify({'error': '採点結果の解析に失敗しました'}), 500

    if answer.strip():
        raw = result.get('score', 0)
        base = 45 if companion == 'dragon' else 40
        result['score'] = min(100, round(base + raw * 0.6))
        grades = [('S',90),('A',75),('B',60),('C',40),('D',0)]
        result['grade'] = next(g for g, t in grades if result['score'] >= t)
        earned = COIN_MAP.get(result['grade'], 10)
        result['coins_earned'] = earned
        conn2 = get_db()
        conn2.execute('UPDATE wallet SET balance = balance + ? WHERE id=1', (earned,))
        conn2.commit()
        conn2.close()
    _save_attempt(qid, result, answer)
    return jsonify(result)

def _save_attempt(qid, d, answer):
    conn = get_db()
    score = d.get('score', 0)
    conn.execute(
        'INSERT INTO attempts(question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,attempted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (qid, score, d.get('grade',''),
         answer, d.get('model_answer',''),
         json.dumps(d.get('covered',[]), ensure_ascii=False),
         json.dumps(d.get('partial',[]), ensure_ascii=False),
         json.dumps(d.get('missed',[]),  ensure_ascii=False),
         d.get('feedback',''), d.get('advice',''),
         datetime.now().strftime('%Y-%m-%d %H:%M')))
    conn.execute('''INSERT INTO results(question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,saved_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(question_id) DO UPDATE SET
            score=excluded.score, grade=excluded.grade, answer=excluded.answer,
            model_answer=excluded.model_answer, covered=excluded.covered,
            partial=excluded.partial, missed=excluded.missed,
            feedback=excluded.feedback, advice=excluded.advice, saved_at=excluded.saved_at''',
        (qid, score, d.get('grade',''),
         answer, d.get('model_answer',''),
         json.dumps(d.get('covered',[]), ensure_ascii=False),
         json.dumps(d.get('partial',[]),  ensure_ascii=False),
         json.dumps(d.get('missed',[]),   ensure_ascii=False),
         d.get('feedback',''), d.get('advice',''),
         datetime.now().strftime('%Y-%m-%d %H:%M')))
    conn.commit()
    conn.close()

# ── Results / Attempts API ────────────────────────────────────
@app.route('/api/results')
def get_results():
    conn = get_db()
    rows = conn.execute('SELECT * FROM results').fetchall()
    conn.close()
    return jsonify({r['question_id']: {
        'score': r['score'], 'grade': r['grade'],
        'answer': r['answer'], 'model_answer': r['model_answer'],
        'saved_at': r['saved_at'],
        'covered': json.loads(r['covered'] or '[]'),
        'partial': json.loads(r['partial'] or '[]'),
        'missed':  json.loads(r['missed']  or '[]'),
        'feedback': r['feedback'] or '',
        'advice':   r['advice']   or '',
    } for r in rows})

@app.route('/api/attempts')
def get_attempts():
    conn = get_db()
    rows = conn.execute(
        'SELECT question_id,score,grade,answer,covered,partial,missed,feedback,advice,attempted_at FROM attempts ORDER BY attempted_at ASC'
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r['question_id'], []).append({
            'score':    r['score'],
            'grade':    r['grade'],
            'at':       r['attempted_at'],
            'answer':   r['answer'] or '',
            'covered':  json.loads(r['covered'] or '[]'),
            'partial':  json.loads(r['partial'] or '[]'),
            'missed':   json.loads(r['missed']  or '[]'),
            'feedback': r['feedback'] or '',
            'advice':   r['advice']   or '',
        })
    return jsonify(result)

# ── File Upload & Extraction ──────────────────────────────────
def _call_extract(blocks, user_key):
    client = anthropic.Anthropic(api_key=user_key)
    msg = client.messages.create(model='claude-sonnet-4-6', max_tokens=4096,
        messages=[{'role':'user','content':blocks}])
    text = msg.content[0].text.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    return json.loads(m.group() if m else text)

@app.route('/api/upload-questions', methods=['POST'])
def upload_questions():
    user_key = ANTHROPIC_API_KEY
    if not user_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('extract')
    if not ok:
        return _rate_limit_response('extract', current, limit)
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'ファイルがありません'}), 400
    name = f.filename.lower()
    data = f.read()
    try:
        if name.endswith(('.jpg','.jpeg','.png','.webp','.gif')):
            mt = ('image/jpeg' if name.endswith(('.jpg','.jpeg')) else
                  'image/png'  if name.endswith('.png')  else
                  'image/webp' if name.endswith('.webp') else 'image/gif')
            blocks = [
                {'type':'image','source':{'type':'base64','media_type':mt,'data':base64.standard_b64encode(data).decode()}},
                {'type':'text','text':EXTRACT_PROMPT}]
        elif name.endswith('.pdf'):
            blocks = [
                {'type':'document','source':{'type':'base64','media_type':'application/pdf','data':base64.standard_b64encode(data).decode()}},
                {'type':'text','text':EXTRACT_PROMPT}]
        elif name.endswith(('.html','.htm')):
            class _S(HTMLParser):
                def __init__(self): super().__init__(); self.p=[]
                def handle_data(self,d): self.p.append(d)
            p=_S(); p.feed(data.decode('utf-8','ignore'))
            blocks=[{'type':'text','text':EXTRACT_PROMPT+'\n\n'+' '.join(p.p)[:12000]}]
        elif name.endswith(('.xlsx','.xls')):
            import openpyxl
            wb=openpyxl.load_workbook(BytesIO(data),data_only=True)
            lines=[]
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    r=[str(c) for c in row if c is not None]
                    if r: lines.append('\t'.join(r))
            blocks=[{'type':'text','text':EXTRACT_PROMPT+'\n\n'+'\n'.join(lines)[:12000]}]
        elif name.endswith('.docx'):
            import docx
            doc=docx.Document(BytesIO(data))
            text='\n'.join(p.text for p in doc.paragraphs if p.text.strip())
            blocks=[{'type':'text','text':EXTRACT_PROMPT+'\n\n'+text[:12000]}]
        else:
            return jsonify({'error': 'PDF/画像/HTML/Excel/Word のみ対応'}), 400
        return jsonify({'questions': _call_extract(blocks, user_key)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Vision Quiz (写真→AIクイズ化) ────────────────────────────
def _call_vision_quiz(image_bytes, media_type, user_key):
    client = anthropic.Anthropic(api_key=user_key)
    msg = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=2048,
        messages=[{'role':'user','content':[
            {'type':'image','source':{'type':'base64','media_type':media_type,'data':base64.standard_b64encode(image_bytes).decode()}},
            {'type':'text','text':VISION_QUIZ_PROMPT},
        ]}])
    text = msg.content[0].text.strip()
    m = re.search(r'\{.*\}', text, re.DOTALL)
    return json.loads(m.group() if m else text)

@app.route('/api/vision-quiz', methods=['POST'])
def vision_quiz():
    user_key = ANTHROPIC_API_KEY
    if not user_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('extract')
    if not ok:
        return _rate_limit_response('extract', current, limit)
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'ファイルがありません'}), 400
    name = (f.filename or '').lower()
    data = f.read()
    if not name.endswith(('.jpg','.jpeg','.png','.webp','.gif')):
        return jsonify({'error': '画像のみ対応（jpg/png/webp/gif）'}), 400
    mt = ('image/jpeg' if name.endswith(('.jpg','.jpeg')) else
          'image/png'  if name.endswith('.png')  else
          'image/webp' if name.endswith('.webp') else 'image/gif')
    try:
        result = _call_vision_quiz(data, mt, user_key)
        return jsonify({'ok': True, **result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── Ghost Messages API ────────────────────────────────────────
@app.route('/api/ghost/<int:square>')
def get_ghost(square):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM ghost_messages WHERE square=? ORDER BY likes DESC, id ASC LIMIT 5',
        (square,)).fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'square': r['square'], 'message': r['message'],
        'author': r['author'] or '名もなき旅人',
        'likes': r['likes'], 'created_at': r['created_at'],
    } for r in rows])

NG_WORDS = [
    # 性的表現
    'セックス','sex','エロ','ero','ポルノ','porn','naked','nude','ヌード',
    'おっぱい','ちんこ','まんこ','ちんぽ','アナル','anal','フェラ','手マン',
    'オナニー','masturbat','レイプ','rape','援交','売春','prostitut',
    'エッチ','hentai','変態','淫','性器','陰茎','陰部','膣','射精',
    # 暴力・差別
    '死ね','殺す','殺せ','ぶっ殺','くたばれ','うせろ',
    'バカ','馬鹿','アホ','クソ','ゴミ','カス','きもい','気持ち悪い','うざい',
    'クズ','ブス','デブ','チビ','障害者','キチガイ','精神病',
    '差別','ヘイト','hate','racist',
    # 個人情報・スパム
    'http','https','www','\.com','\.net','\.jp','LINE','twitter','instagram',
    # その他問題ワード
    '薬物','覚醒剤','大麻','マリファナ','麻薬','drug',
    'パスワード','password','クレジット','カード番号',
    # SNS・連絡先誘導
    'line id','line@','lineID','ラインID','ラインid',
    '@gmail','@yahoo','@icloud','@hotmail','@outlook',
    'discord','discordid','discord.gg',
    'telegram','テレグラム',
    'wechat','ウィーチャット','微信',
    'kakao','カカオ',
    'snapchat','スナチャ',
    'skype','スカイプ',
    'dm送','dm下さい','連絡ください','連絡して','連絡先',
    'id教え','id送','アカウント教え',
]

_EMAIL_RE   = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}')
_ACCOUNT_RE = re.compile(r'@[a-zA-Z0-9_.]{3,}')  # @username 形式のアカウントID

# 薬物隠語（食べ物系）— 取引ワードと組み合わせた時のみブロック
_DRUG_SLANG  = ['アイス', 'トマト', 'ブロッコリー', 'バナナ', 'シャブ', 'ヤク']
_DEAL_WORDS  = ['買', '売', '譲', '入手', '取引', '仕入', '注文', 'ください', '欲しい',
                'どこで', '値段', '円', '個', 'グラム', 'g ', '連絡', 'dm', 'DM']

def _contains_drug_slang_combo(text: str) -> bool:
    has_slang = any(s in text for s in _DRUG_SLANG)
    has_deal  = any(d in text for d in _DEAL_WORDS)
    return has_slang and has_deal

def _contains_ng(text: str) -> bool:
    t = text.lower()
    if any(re.search(ng.lower(), t) for ng in NG_WORDS):
        return True
    if _EMAIL_RE.search(text):
        return True
    if _ACCOUNT_RE.search(text):
        return True
    # 電話番号: 数字とハイフン・括弧だけで10文字以上連続
    digits_only = re.sub(r'[\-\(\)\s\+]', '', text)
    if re.search(r'\d{10,}', digits_only):
        return True
    if _contains_drug_slang_combo(text):
        return True
    return False

@app.route('/api/ghost', methods=['POST'])
def post_ghost():
    d = request.get_json(force=True)
    square  = d.get('square')
    message = (d.get('message') or '').strip()
    author  = (d.get('author') or '名もなき旅人').strip()
    if not square or not message:
        return jsonify({'error': 'square と message は必須です'}), 400
    if len(message) > 100:
        return jsonify({'error': 'メッセージは100文字以内にしてください'}), 400
    if _contains_ng(message) or _contains_ng(author):
        return jsonify({'error': '不適切な表現が含まれています。言葉を選んで刻んでください。'}), 400
    conn = get_db()
    conn.execute('INSERT INTO ghost_messages(square,message,author,created_at) VALUES(?,?,?,?)',
        (square, message, author, datetime.now().strftime('%Y-%m-%d %H:%M')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/ghost/<int:msg_id>/like', methods=['POST'])
def like_ghost(msg_id):
    conn = get_db()
    conn.execute('UPDATE ghost_messages SET likes=likes+1 WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/ghost/<int:msg_id>/seal', methods=['POST'])
def seal_ghost(msg_id):
    conn = get_db()
    conn.execute('DELETE FROM ghost_messages WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Stats API ─────────────────────────────────────────────────
@app.route('/api/stats')
def get_stats():
    conn = get_db()
    decks = conn.execute('SELECT * FROM decks').fetchall()
    ADVANCE = {'S': 5, 'A': 4, 'B': 3, 'C': 2, 'D': 1}
    GRADE_ORDER = ['S', 'A', 'B', 'C', 'D']
    deck_stats = []
    total_attempts = 0
    total_score_sum = 0
    board_pos = 0
    for d in decks:
        qids = [r['id'] for r in conn.execute(
            'SELECT id FROM questions WHERE deck_id=?', (d['id'],)).fetchall()]
        if not qids:
            continue
        ph = ','.join('?' * len(qids))
        attempts = conn.execute(
            f'SELECT score,grade FROM attempts WHERE question_id IN ({ph})', qids).fetchall()
        if not attempts:
            continue
        count = len(attempts)
        scores = [a['score'] for a in attempts]
        grades = [a['grade'] for a in attempts]
        avg = round(sum(scores) / count)
        best = min(grades, key=lambda g: GRADE_ORDER.index(g) if g in GRADE_ORDER else 99)
        accuracy = round(sum(1 for g in grades if g in ('S', 'A')) / count * 100)
        pos_delta = sum(ADVANCE.get(g, 1) for g in grades)
        total_attempts += count
        total_score_sum += sum(scores)
        board_pos += pos_delta
        deck_stats.append({
            'deck_id':      d['id'],
            'deck_name':    d['name'],
            'category':     d['category'] or '',
            'attempt_count': count,
            'avg_score':    avg,
            'best_grade':   best,
            'accuracy':     accuracy,
            'grade_counts': {g: grades.count(g) for g in GRADE_ORDER},
        })
    conn.close()
    raw_board_pos = board_pos
    board_total = 300
    board_pos = min(board_pos, board_total)
    return jsonify({
        'board_pos':      board_pos,
        'board_pct':      round(board_pos / board_total * 100),
        'raw_board_pos':  raw_board_pos,
        'total_attempts': total_attempts,
        'avg_score':      round(total_score_sum / total_attempts) if total_attempts else 0,
        'decks':          deck_stats,
    })

# ── Wallet ───────────────────────────────────────────────────
@app.route('/api/wallet')
def get_wallet():
    conn = get_db()
    row = conn.execute('SELECT balance FROM wallet WHERE id=1').fetchone()
    conn.close()
    return jsonify({'balance': row['balance'] if row else 0})

@app.route('/api/wallet/spend', methods=['POST'])
def spend_wallet():
    d = request.get_json(force=True)
    cost = int(d.get('cost', 0))
    conn = get_db()
    row = conn.execute('SELECT balance FROM wallet WHERE id=1').fetchone()
    balance = row['balance'] if row else 0
    if balance < cost:
        conn.close()
        return jsonify({'error': '石が足りません'}), 400
    conn.execute('UPDATE wallet SET balance = balance - ? WHERE id=1', (cost,))
    conn.commit()
    new_balance = conn.execute('SELECT balance FROM wallet WHERE id=1').fetchone()['balance']
    conn.close()
    return jsonify({'balance': new_balance})

@app.route('/api/wallet/earn', methods=['POST'])
def earn_wallet():
    d = request.get_json(force=True)
    amount = int(d.get('amount', 0))
    conn = get_db()
    conn.execute('UPDATE wallet SET balance = balance + ? WHERE id=1', (amount,))
    conn.commit()
    new_balance = conn.execute('SELECT balance FROM wallet WHERE id=1').fetchone()['balance']
    conn.close()
    return jsonify({'balance': new_balance})

@app.route('/api/murmur-fragments')
def murmur_fragments():
    """病的モード用：問題文・キーポイントからランダムな断片を返す"""
    conn = get_db()
    rows = conn.execute(
        'SELECT question, key_points FROM questions ORDER BY RANDOM() LIMIT 30'
    ).fetchall()
    conn.close()
    fragments = []
    import json as _json
    for r in rows:
        # 問題文の先頭20〜30文字
        q = (r['question'] or '').strip()
        if len(q) >= 6:
            end = min(len(q), 22)
            fragments.append(q[:end] + '…')
        # キーポイントから最初の項目
        kp = r['key_points']
        if kp:
            try:
                pts = _json.loads(kp)
                if isinstance(pts, list) and pts:
                    t = (pts[0].get('t') or '').strip()
                    if len(t) >= 4:
                        fragments.append(t[:20] + '…' if len(t) > 20 else t)
            except Exception:
                pass
    import random
    random.shuffle(fragments)
    return jsonify(fragments[:20])

@app.route('/api/achievements/unlock', methods=['POST'])
def unlock_achievements():
    d = request.get_json(force=True)
    user_token = d.get('user_token', '')
    ids = d.get('achievement_ids', [])
    if not user_token or not ids:
        return jsonify({'ok': False}), 400
    conn = get_db()
    for aid in ids:
        conn.execute(
            'INSERT OR IGNORE INTO achievement_unlocks(achievement_id, user_token) VALUES(?,?)',
            (aid, user_token)
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/achievements/rarity')
def achievement_rarity():
    conn = get_db()
    total_users = conn.execute(
        'SELECT COUNT(DISTINCT user_token) FROM achievement_unlocks'
    ).fetchone()[0]
    if total_users == 0:
        conn.close()
        return jsonify({})
    rows = conn.execute(
        'SELECT achievement_id, COUNT(DISTINCT user_token) as cnt FROM achievement_unlocks GROUP BY achievement_id'
    ).fetchall()
    conn.close()
    result = {r['achievement_id']: round(r['cnt'] / total_users * 100) for r in rows}
    return jsonify(result)

@app.route('/api/questions/<int:q_id>/hint')
def get_hint(q_id):
    conn = get_db()
    row = conn.execute('SELECT key_points, flowchart, category FROM questions WHERE id=?', (q_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    kps = json.loads(row['key_points'] or '[]')
    hints = [k['t'] for k in kps if isinstance(k, dict) and k.get('t')]
    return jsonify({
        'category': row['category'],
        'flowchart': row['flowchart'] or '',
        'key_count': len(hints),
        'first_hint': hints[0] if hints else '',
    })

# ── MCQ ──────────────────────────────────────────────────────
def _normalize_mcq_payload(data):
    options = data.get('options') or []
    if len(options) != 5:
        raise ValueError('MCQ options must contain exactly 5 choices')
    if isinstance(data.get('correct_indices'), list):
        correct_indices = sorted({int(i) for i in data['correct_indices'] if 0 <= int(i) < len(options)})
    elif 'correct_index' in data:
        correct_indices = [int(data['correct_index'])]
    else:
        raise ValueError('MCQ payload must include correct_indices or correct_index')
    if len(correct_indices) not in (1, 2):
        raise ValueError('MCQ must have one or two correct choices')
    return {'options': options, 'correct_index': correct_indices[0], 'correct_indices': correct_indices}

def _shuffle_mcq(options, correct_indices):
    import random
    correct_texts = [options[i] for i in correct_indices]
    shuffled = list(options)
    random.shuffle(shuffled)
    return {
        'options': shuffled,
        'correct_index': shuffled.index(correct_texts[0]),
        'correct_indices': sorted(shuffled.index(text) for text in correct_texts),
    }

def _mcq_cache_matches(payload, requested_count):
    if requested_count not in (1, 2):
        return True
    try:
        normalized = _normalize_mcq_payload(payload)
        return len(normalized['correct_indices']) == requested_count
    except Exception:
        return False

@app.route('/api/questions/<int:q_id>/mcq')
def get_mcq(q_id):
    api_key = ANTHROPIC_API_KEY
    correct_count_raw = request.args.get('correct_count', 'mixed')
    requested_count = int(correct_count_raw) if correct_count_raw in ('1', '2') else None
    conn = get_db()
    row = conn.execute('SELECT * FROM questions WHERE id=?', (q_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if row['mcq_options']:
        try:
            cached = json.loads(row['mcq_options'])
            if len(cached.get('options', [])) == 5 and _mcq_cache_matches(cached, requested_count):
                cached = _normalize_mcq_payload(cached)
                conn.close()
                return jsonify(cached)
        except Exception:
            pass
    if not api_key:
        conn.close()
        return jsonify({'error': 'サーバー側のAPIキーが未設定です'}), 503
    ok, current, limit = _check_rate('ai')
    if not ok:
        conn.close()
        return _rate_limit_response('ai', current, limit)
    import random
    kps = json.loads(row['key_points'] or '[]')
    answer_text = row['model_answer'] or '\n'.join(
        f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
    client = anthropic.Anthropic(api_key=api_key)
    correct_rule = '設問への答えとして選ぶべき選択肢は必ず2つ。2つとも correct_indices に入れる' if requested_count == 2 else (
        '設問への答えとして選ぶべき選択肢は必ず1つ' if requested_count == 1 else
        '設問への答えとして選ぶべき選択肢は1つまたは2つ。2つ選ぶべき問題では両方を correct_indices に入れる'
    )
    prompt = f"""以下の記述式問題と正解をもとに、5択選択肢を日本語で作成してください。

問題: {row['question']}

正解の要点:
{answer_text[:600]}

要件:
- 選択肢は5つ
- {correct_rule}
- correct_indices は「設問への答えとして選ぶべき選択肢」の番号にする
- 問題文が「誤り」「適切でない」「最も適切でない」などを問う場合は、誤っている／適切でない選択肢を correct_indices に入れる
- correct_indices に入れる選択肢は模範解答の核心を1〜2文で簡潔に反映する
- correct_indices 以外の選択肢は、それぞれ異なる方向の非該当選択肢にする
- 各選択肢は30〜60字程度

必ずこのJSONのみを返してください（前後にテキスト不要）:
{{"options":["選ぶべき選択肢1","選ぶべき選択肢2または非該当","非該当1","非該当2","非該当3"],"correct_indices":[0,1]}}"""
    try:
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}])
        raw = msg.content[0].text.strip()
        data = json.loads(re.search(r'\{.*\}', raw, re.S).group())
        normalized = _normalize_mcq_payload(data)
        result = _shuffle_mcq(normalized['options'], normalized['correct_indices'])
        conn.execute('UPDATE questions SET mcq_options=? WHERE id=?', (json.dumps(result), q_id))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
    conn.close()
    return jsonify(result)

@app.route('/api/mcq/clear-cache', methods=['POST'])
def clear_mcq_cache():
    """mcq_options を全てNULLにリセット（択数変更時などに使用）。"""
    conn = get_db()
    conn.execute('UPDATE questions SET mcq_options=NULL')
    conn.commit()
    count = conn.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'cleared': count})

@app.route('/api/mcq/generate-all', methods=['POST'])
def generate_all_mcq():
    """mcq_options が NULL の全問題に対して一括生成してキャッシュする。"""
    d = request.get_json(force=True)
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'サーバー側のAPIキーが未設定です'}), 400
    import random
    conn = get_db()
    rows = conn.execute(
        'SELECT id, question, model_answer, key_points FROM questions WHERE mcq_options IS NULL'
    ).fetchall()
    conn.close()
    total = len(rows)
    if total == 0:
        return jsonify({'ok': True, 'generated': 0, 'message': 'すでに全問キャッシュ済みです'})
    client = anthropic.Anthropic(api_key=api_key)
    ok = err = 0
    for row in rows:
        kps = json.loads(row['key_points'] or '[]')
        answer_text = row['model_answer'] or '\n'.join(
            f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
        prompt = f"""以下の記述式問題と正解をもとに、5択選択肢を日本語で作成してください。

問題: {row['question']}

正解の要点:
{answer_text[:600]}

要件:
- 選択肢は5つ
- 設問への答えとして選ぶべき選択肢は1つまたは2つ。2つ選ぶべき問題では両方を correct_indices に入れる
- correct_indices は「設問への答えとして選ぶべき選択肢」の番号にする
- 問題文が「誤り」「適切でない」「最も適切でない」などを問う場合は、誤っている／適切でない選択肢を correct_indices に入れる
- correct_indices に入れる選択肢は模範解答の核心を1〜2文で簡潔に反映する
- correct_indices 以外の選択肢は、それぞれ異なる方向の非該当選択肢にする
- 各選択肢は30〜60字程度

必ずこのJSONのみを返してください（前後にテキスト不要）:
{{"options":["選ぶべき選択肢1","選ぶべき選択肢2または非該当","非該当1","非該当2","非該当3"],"correct_indices":[0,1]}}"""
        try:
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001', max_tokens=800,
                messages=[{'role': 'user', 'content': prompt}])
            raw = msg.content[0].text.strip()
            data = json.loads(re.search(r'\{.*\}', raw, re.S).group())
            normalized = _normalize_mcq_payload(data)
            result = _shuffle_mcq(normalized['options'], normalized['correct_indices'])
            c = get_db()
            c.execute('UPDATE questions SET mcq_options=? WHERE id=?',
                      (json.dumps(result, ensure_ascii=False), row['id']))
            c.commit()
            c.close()
            ok += 1
        except Exception:
            err += 1
    return jsonify({'ok': True, 'generated': ok, 'errors': err, 'total': total})

@app.route('/api/score-mcq', methods=['POST'])
def score_mcq():
    d = request.get_json(force=True)
    q_id    = int(d['question_id'])
    correct = bool(d['correct'])
    conn = get_db()
    row = conn.execute('SELECT model_answer FROM questions WHERE id=?', (q_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.close()
    score = 100 if correct else 0
    grade = 'S' if correct else 'D'
    coins = COIN_MAP.get(grade, 10)
    result = {
        'score': score, 'grade': grade,
        'model_answer': row['model_answer'] or '',
        'covered': [], 'partial': [], 'missed': [],
        'feedback': '正解です！' if correct else '不正解。解説を確認しましょう。',
        'advice': '', 'coins_earned': coins,
    }
    _save_attempt(q_id, result, '5択:正解' if correct else '5択:不正解')
    conn2 = get_db()
    conn2.execute('UPDATE wallet SET balance = balance + ? WHERE id=1', (coins,))
    conn2.commit()
    new_balance = conn2.execute('SELECT balance FROM wallet WHERE id=1').fetchone()['balance']
    conn2.close()
    result['balance'] = new_balance
    return jsonify(result)

# ── Companion reaction ───────────────────────────────────────
COMPANION_SYSTEM = """あなたは旅の仲間の小動物です。プレイヤーの言葉に対して1〜2文で短く反応してください。

キャラクター:
- 好奇心旺盛で忠実、時々ちょっとズレた反応をする
- 勉強・挑戦には熱く背中を押す
- 休憩・遊びには少し心配しつつも応援する
- 驚いたり、予想外のことには大げさに反応する
- 語尾は「〜だよ」「〜かな」「〜！」などかわいい話し言葉
- 絵文字は使わない
- 返答はかならず1〜2文のみ"""

@app.route('/api/companion/react', methods=['POST'])
def companion_react():
    d = request.get_json(force=True)
    player_input = (d.get('player_input') or '').strip()
    api_key = ANTHROPIC_API_KEY
    if not player_input:
        return jsonify({'error': 'player_input required'}), 400
    if not api_key:
        return jsonify({'error': 'サーバー側のAPIキーが未設定です'}), 401
    ok, current, limit = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            system=COMPANION_SYSTEM,
            messages=[{'role': 'user', 'content': player_input}])
        return jsonify({'reaction': msg.content[0].text.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Backup / Restore ─────────────────────────────────────────
@app.route('/api/backup')
def backup():
    conn = get_db()
    data = {
        'decks':          [dict(r) for r in conn.execute('SELECT * FROM decks').fetchall()],
        'questions':      [dict(r) for r in conn.execute('SELECT * FROM questions').fetchall()],
        'attempts':       [dict(r) for r in conn.execute('SELECT * FROM attempts').fetchall()],
        'results':        [dict(r) for r in conn.execute('SELECT * FROM results').fetchall()],
        'ghost_messages': [dict(r) for r in conn.execute('SELECT * FROM ghost_messages').fetchall()],
        'wallet':         [dict(r) for r in conn.execute('SELECT * FROM wallet').fetchall()],
        'achievement_unlocks': [dict(r) for r in conn.execute('SELECT * FROM achievement_unlocks').fetchall()],
    }
    conn.close()
    return jsonify(data)

def _table_cols(conn, table):
    return [r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]

def _insert_replace_row(conn, table, row):
    cols = [c for c in _table_cols(conn, table) if c in row]
    if not cols:
        return
    placeholders = ','.join(['?'] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({placeholders})"
    conn.execute(sql, [row.get(c) for c in cols])

@app.route('/api/restore', methods=['POST'])
def restore():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'バックアップJSONを読み取れません'}), 400
    if not isinstance(data, dict):
        return jsonify({'error': 'バックアップ形式が不正です'}), 400
    conn = get_db()
    try:
        for table in ['decks', 'questions', 'attempts', 'results', 'ghost_messages', 'wallet', 'achievement_unlocks']:
            rows = data.get(table, [])
            if rows is None:
                continue
            if not isinstance(rows, list):
                return jsonify({'error': f'{table} の形式が不正です'}), 400
            for row in rows:
                if isinstance(row, dict):
                    _insert_replace_row(conn, table, row)
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': f'復元に失敗しました: {e}'}), 500
    finally:
        conn.close()

# ── Score inline (similar questions, no DB entry needed) ─────
@app.route('/api/score-inline', methods=['POST'])
def score_inline():
    d          = request.get_json(force=True)
    question   = (d.get('question') or '').strip()
    model_ans  = (d.get('model_answer') or '').strip()
    key_points = d.get('key_points', [])
    answer     = (d.get('answer') or '').strip()
    user_key   = ANTHROPIC_API_KEY
    companion  = d.get('companion')
    if not answer:
        return jsonify({'error': '回答を入力してください'}), 400
    if not user_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('score')
    if not ok:
        return _rate_limit_response('score', current, limit)
    kp_str = '\n'.join(f"- (w=2) {k['t']}" for k in key_points if isinstance(k, dict) and k.get('t'))
    client = anthropic.Anthropic(api_key=user_key)
    msg = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=2000, temperature=0,
        system=SCORE_SYSTEM,
        messages=[{'role':'user','content':
            f"【問題】{question}\n\n【採点キーポイント】\n{kp_str}\n\n【受験者の回答】\n{answer}"}])
    text = msg.content[0].text.strip()
    try:
        result = json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(m.group()) if m else {}
    if not result:
        return jsonify({'error': '採点結果の解析に失敗しました'}), 500
    raw = result.get('score', 0)
    base = 45 if companion == 'dragon' else 40
    result['score'] = min(100, round(base + raw * 0.6))
    grades = [('S',90),('A',75),('B',60),('C',40),('D',0)]
    result['grade'] = next(g for g, t in grades if result['score'] >= t)
    result['model_answer'] = model_ans
    result['coins_earned'] = 0
    return jsonify(result)

# ── Weakness radar ───────────────────────────────────────────
@app.route('/api/weakness-radar')
def weakness_radar():
    conn = get_db()
    rows = conn.execute('''
        SELECT q.category,
               COUNT(a.id)   AS attempt_count,
               ROUND(AVG(a.score), 1) AS avg_score,
               ROUND(SUM(CASE WHEN a.grade IN ('S','A') THEN 1 ELSE 0 END) * 100.0 / COUNT(a.id), 1) AS high_pct
        FROM attempts a
        JOIN questions q ON a.question_id = q.id
        WHERE q.category IS NOT NULL AND q.category != ''
        GROUP BY q.category
        HAVING COUNT(a.id) >= 1
        ORDER BY avg_score ASC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── Mistake cards ─────────────────────────────────────────────
@app.route('/api/mistake-cards')
def mistake_cards():
    conn = get_db()
    rows = conn.execute('''
        SELECT q.id, q.question, q.key_points, q.category,
               ROUND(AVG(a.score), 1) AS avg_score,
               MIN(a.grade) AS worst_grade,
               COUNT(a.id)  AS attempt_count
        FROM questions q
        JOIN attempts a ON a.question_id = q.id
        GROUP BY q.id
        HAVING avg_score < 65
        ORDER BY avg_score ASC
        LIMIT 30
    ''').fetchall()
    conn.close()
    cards = []
    for r in rows:
        try:
            kps = json.loads(r['key_points'] or '[]')
            key_text = ' / '.join(k['t'] for k in kps[:4] if isinstance(k, dict) and k.get('t'))
        except Exception:
            key_text = ''
        cards.append({
            'question_id':   r['id'],
            'question':      r['question'],
            'key_summary':   key_text,
            'category':      r['category'] or '',
            'avg_score':     r['avg_score'] or 0,
            'worst_grade':   r['worst_grade'] or '?',
            'attempt_count': r['attempt_count'],
        })
    return jsonify(cards)

# ── Similar question ──────────────────────────────────────────
@app.route('/api/questions/<int:qid>/similar', methods=['POST'])
def similar_question(qid):
    data    = request.get_json(force=True)
    api_key = ANTHROPIC_API_KEY
    conn    = get_db()
    row     = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '問題が見つかりません'}), 404
    if not api_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    ok, current, limit = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit)
    try:
        kps = json.loads(row['key_points'] or '[]')
        kp_text = '\n'.join(f'・{k["t"]}' for k in kps[:5] if isinstance(k, dict) and k.get('t'))
    except Exception:
        kp_text = row['model_answer'][:300]
    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""以下の記述式問題と類似した、同じ知識領域だが異なる切り口の問題を1つ作成してください。

元の問題: {row['question']}
カテゴリ: {row['category']}
正解の要点:
{kp_text}

要件:
- 同じ知識を問うが、問い方・状況設定・視点を変える
- 記述式（選択肢なし）
- 難易度は同程度
- key_pointsは3〜5個

必ずこのJSONのみを返してください（前後にテキスト不要）:
{{"question":"問題文","model_answer":"模範解答（100字以内）","key_points":[{{"t":"要点1"}},{{"t":"要点2"}},{{"t":"要点3"}}],"category":"{row['category']}"}}"""
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001', max_tokens=600,
        messages=[{'role': 'user', 'content': prompt}])
    raw    = msg.content[0].text.strip()
    result = json.loads(re.search(r'\{.*\}', raw, re.S).group())
    return jsonify(result)

# ── Wipe all user data ────────────────────────────────────────
@app.route('/api/wipe', methods=['POST'])
def wipe_all():
    d = request.get_json(silent=True) or {}
    if d.get('confirm') != 'DELETE_ALL_DATA':
        return jsonify({'error': 'confirmation token required'}), 400
    conn = get_db()
    counts = {}
    for table in ('attempts', 'results', 'questions', 'decks',
                  'ghost_messages', 'achievement_unlocks'):
        row = conn.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()
        counts[table] = row['n']
        conn.execute(f'DELETE FROM {table}')
    conn.execute('UPDATE wallet SET balance=0 WHERE id=1')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'deleted': counts})

# ── Health check ──────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '1.0.0'})

# ── Legal pages ───────────────────────────────────────────────
LEGAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'legal')

def _serve_legal(filename: str):
    path = os.path.join(LEGAL_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            html = f.read()
        return Response(html, mimetype='text/html; charset=utf-8')
    except FileNotFoundError:
        return Response('Not Found', status=404)

@app.route('/privacy')
def privacy():
    return _serve_legal('privacy.html')

@app.route('/support')
def support():
    return _serve_legal('support.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
