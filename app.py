import os, json, re, sqlite3, base64, hashlib
from datetime import datetime
from io import BytesIO
from html.parser import HTMLParser
from flask import Flask, request, Response, stream_with_context, jsonify
import anthropic

app = Flask(__name__)

@app.after_request
def _no_cache_html_json(resp):
    ct = resp.headers.get('Content-Type', '')
    if 'html' in ct or 'json' in ct:
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp

PORT      = int(os.environ.get('PORT', 8000))
DATA_DIR  = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
RESULTS_DB = os.path.join(DATA_DIR, 'results.db')
os.makedirs(DATA_DIR, exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ADMIN_API_TOKEN = os.environ.get('GRADIFY_ADMIN_TOKEN', '')
LEGACY_USER_ID = 'legacy'

# ── CORS ──────────────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Gradify-User-ID, X-Admin-Token'
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
    'vision_quiz': int(os.environ.get('RATE_LIMIT_VISION_QUIZ', 3)),
    'upload_questions': int(os.environ.get('RATE_LIMIT_UPLOAD_QUESTIONS', 5)),
}

TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', 7))
DAILY_AI_LIMIT = int(os.environ.get('DAILY_AI_LIMIT', 5))
# サブスクのティア別 1日上限 (AI採点・MCQ生成・写真クイズ等のAIコール数)
# シンプル採点モードはこの枠を消費しない
SUB_LIGHT_DAILY_LIMIT    = int(os.environ.get('SUB_LIGHT_DAILY_LIMIT', 7))
SUB_STANDARD_DAILY_LIMIT = int(os.environ.get('SUB_STANDARD_DAILY_LIMIT', 15))
SUB_PRO_DAILY_LIMIT      = int(os.environ.get('SUB_PRO_DAILY_LIMIT', 33))

def _client_id():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def _user_id():
    raw = (
        request.headers.get('X-Gradify-User-ID')
        or (request.get_json(silent=True) or {}).get('user_id')
        or LEGACY_USER_ID
    )
    user_id = re.sub(r'[^A-Za-z0-9_.:-]', '', str(raw))[:80]
    return user_id or LEGACY_USER_ID

def _require_admin():
    if not ADMIN_API_TOKEN:
        return jsonify({'error': '教材マスターの編集は管理者のみ可能です'}), 403
    token = request.headers.get('X-Admin-Token') or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if token != ADMIN_API_TOKEN:
        return jsonify({'error': '教材マスターの編集は管理者のみ可能です'}), 403
    return None

def _ensure_wallet(conn, user_id):
    conn.execute(
        'INSERT OR IGNORE INTO wallet(user_id, balance) VALUES(?, 0)',
        (user_id,)
    )

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

def _check_trial(user_id):
    """Returns dict with status: active|expired|subscribed, days_left, subscribed, tier, first_use_at."""
    conn = get_db()
    row = conn.execute('SELECT first_use_at, subscribed, subscribed_tier FROM user_trial WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        first = datetime.now().isoformat(timespec='seconds')
        conn.execute('INSERT INTO user_trial(user_id, first_use_at, subscribed, subscribed_tier) VALUES(?, ?, 0, NULL)', (user_id, first))
        conn.commit()
        conn.close()
        return {'status': 'active', 'days_left': TRIAL_DAYS, 'subscribed': False, 'tier': None, 'first_use_at': first}
    tier = row['subscribed_tier'] if 'subscribed_tier' in row.keys() else None
    if row['subscribed']:
        conn.close()
        return {
            'status': 'subscribed',
            'days_left': 0,
            'subscribed': True,
            'tier': (tier or '').lower() or 'standard',
            'first_use_at': row['first_use_at'],
        }
    try:
        first_dt = datetime.fromisoformat(row['first_use_at'])
    except Exception:
        first_dt = datetime.now()
    elapsed_days = (datetime.now() - first_dt).days
    days_left = max(0, TRIAL_DAYS - elapsed_days)
    conn.close()
    return {
        'status': 'active' if days_left > 0 else 'expired',
        'days_left': days_left,
        'subscribed': False,
        'tier': None,
        'first_use_at': row['first_use_at'],
    }

def _trial_expired_response():
    return jsonify({
        'ok': False,
        'error': 'trial_ended',
        'message': '7日間のお試し期間が終了しました。継続利用にはサブスクが必要です。',
        'subscription_required': True,
    }), 402

def _daily_limit_for(user_id):
    """Returns the user's daily AI limit. tier別の上限を返す。
    無料: DAILY_AI_LIMIT / light: SUB_LIGHT / standard: SUB_STANDARD / pro: SUB_PRO"""
    info = _check_trial(user_id)
    if info.get('subscribed'):
        tier = (info.get('tier') or 'standard').lower()
        if tier == 'pro':
            return SUB_PRO_DAILY_LIMIT
        if tier == 'standard':
            return SUB_STANDARD_DAILY_LIMIT
        if tier == 'light':
            return SUB_LIGHT_DAILY_LIMIT
        return SUB_STANDARD_DAILY_LIMIT
    return DAILY_AI_LIMIT

def _is_unlimited(user_id):
    """Backward-compat. ティア廃止により常に False。"""
    return False

def _daily_ai_used(user_id, day=None):
    day = day or datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute(
        'SELECT count FROM user_daily_ai WHERE user_id=? AND day=?',
        (user_id, day)
    ).fetchone()
    conn.close()
    return row['count'] if row else 0

def _check_daily_ai(user_id):
    """Returns (ok, used, limit). limit=0 のとき無制限。"""
    limit = _daily_limit_for(user_id)
    if limit == 0:
        return True, 0, 0
    day = datetime.now().strftime('%Y-%m-%d')
    used = _daily_ai_used(user_id, day)
    if used >= limit:
        return False, used, limit
    return True, used, limit

def _consume_daily_ai(user_id):
    """Increment daily AI counter. Skips for unlimited users."""
    if _daily_limit_for(user_id) == 0:
        return
    day = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    conn.execute(
        'INSERT INTO user_daily_ai (user_id, day, count) VALUES (?, ?, 1) '
        'ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1',
        (user_id, day)
    )
    conn.commit()
    conn.close()

def _daily_limit_response(used, limit):
    return jsonify({
        'ok': False,
        'error': '本日の無料枠を使い切りました',
        'daily_limit': True,
        'used': used,
        'limit': limit,
        'message': f'1日{limit}回までAI機能を無料で使えます。明日0時にリセットされます。',
    }), 429

@app.route('/api/trial-status')
def trial_status():
    info = _check_trial(_user_id())
    return jsonify({**info, 'trial_days': TRIAL_DAYS})

@app.route('/api/entitlement')
def entitlement():
    user_id = _user_id()
    info = _check_trial(user_id)
    limit = _daily_limit_for(user_id)
    day = datetime.now().strftime('%Y-%m-%d')
    used = _daily_ai_used(user_id, day)
    return jsonify({
        'subscribed': bool(info.get('subscribed')),
        'tier': info.get('tier'),
        # buyout は廃止。互換のため false を返し続ける
        'buyout': False,
        'unlimited': False,
        'daily_used': used,
        'daily_limit': limit,
        'daily_remaining': max(0, limit - used),
    })

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
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            comment TEXT,
            context_json TEXT,
            user_token TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            type TEXT NOT NULL,
            label TEXT NOT NULL,
            summary TEXT,
            body TEXT,
            source_id TEXT,
            metadata TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT
        );
    ''')
    wallet_cols = [r['name'] for r in conn.execute('PRAGMA table_info(wallet)').fetchall()]
    if 'user_id' not in wallet_cols:
        old_wallet = conn.execute('SELECT balance FROM wallet WHERE id=1').fetchone()
        old_balance = old_wallet['balance'] if old_wallet else 0
        conn.execute('''
            CREATE TABLE IF NOT EXISTS wallet_v2 (
                user_id TEXT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            )
        ''')
        conn.execute('INSERT OR REPLACE INTO wallet_v2(user_id, balance) VALUES(?, ?)', (LEGACY_USER_ID, old_balance))
        conn.execute('DROP TABLE wallet')
        conn.execute('ALTER TABLE wallet_v2 RENAME TO wallet')
    _ensure_wallet(conn, LEGACY_USER_ID)

    result_cols = [r['name'] for r in conn.execute('PRAGMA table_info(results)').fetchall()]
    if 'user_id' not in result_cols:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS results_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'legacy',
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
                saved_at TEXT,
                UNIQUE(user_id, question_id)
            )
        ''')
        conn.execute('''
            INSERT INTO results_v2(id,user_id,question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,saved_at)
            SELECT id,?,question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,saved_at FROM results
        ''', (LEGACY_USER_ID,))
        conn.execute('DROP TABLE results')
        conn.execute('ALTER TABLE results_v2 RENAME TO results')

    attempt_cols = [r['name'] for r in conn.execute('PRAGMA table_info(attempts)').fetchall()]
    if 'user_id' not in attempt_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN user_id TEXT DEFAULT 'legacy'")
        conn.execute("UPDATE attempts SET user_id=? WHERE user_id IS NULL OR user_id=''", (LEGACY_USER_ID,))
    # migration: add mcq_options column if missing
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(questions)').fetchall()]
    if 'mcq_options' not in cols:
        conn.execute('ALTER TABLE questions ADD COLUMN mcq_options TEXT DEFAULT NULL')
    # migration: per-user deck/question ownership
    deck_cols = [r['name'] for r in conn.execute('PRAGMA table_info(decks)').fetchall()]
    if 'owner_id' not in deck_cols:
        conn.execute('ALTER TABLE decks ADD COLUMN owner_id TEXT DEFAULT NULL')
    if 'owner_id' not in cols:
        conn.execute('ALTER TABLE questions ADD COLUMN owner_id TEXT DEFAULT NULL')
    if 'difficulty' not in cols:
        conn.execute('ALTER TABLE questions ADD COLUMN difficulty TEXT DEFAULT NULL')
    if 'mcq_correct_count' not in cols:
        conn.execute('ALTER TABLE questions ADD COLUMN mcq_correct_count INTEGER DEFAULT NULL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_trial (
            user_id TEXT PRIMARY KEY,
            first_use_at TEXT NOT NULL,
            subscribed INTEGER NOT NULL DEFAULT 0,
            buyout INTEGER NOT NULL DEFAULT 0
        )
    ''')
    trial_cols = [r['name'] for r in conn.execute('PRAGMA table_info(user_trial)').fetchall()]
    if 'buyout' not in trial_cols:
        conn.execute('ALTER TABLE user_trial ADD COLUMN buyout INTEGER NOT NULL DEFAULT 0')
    if 'subscribed_tier' not in trial_cols:
        conn.execute('ALTER TABLE user_trial ADD COLUMN subscribed_tier TEXT')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_daily_ai (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS vision_quiz_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id TEXT NOT NULL,
            ai_prediction TEXT NOT NULL,
            user_correction TEXT NOT NULL,
            ai_candidates TEXT,
            correct_label TEXT,
            memo TEXT,
            category TEXT,
            confidence REAL,
            feedback_type TEXT,
            created_at TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS vision_quiz_memory_summaries (
            memory_key TEXT PRIMARY KEY,
            category TEXT,
            correct_label TEXT,
            summary TEXT,
            count INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT
        )
    ''')
    correction_cols = [r['name'] for r in conn.execute('PRAGMA table_info(vision_quiz_corrections)').fetchall()]
    for name, coltype in [
        ('ai_candidates', 'TEXT'),
        ('correct_label', 'TEXT'),
        ('memo', 'TEXT'),
        ('category', 'TEXT'),
        ('confidence', 'REAL'),
        ('feedback_type', 'TEXT'),
    ]:
        if name not in correction_cols:
            conn.execute(f'ALTER TABLE vision_quiz_corrections ADD COLUMN {name} {coltype}')
    rows = conn.execute(
        '''
        SELECT id, ai_prediction, user_correction
        FROM vision_quiz_corrections
        WHERE correct_label IS NULL OR memo IS NULL OR category IS NULL
        '''
    ).fetchall()
    for row in rows:
        try:
            ai_prediction = json.loads(row['ai_prediction'] or '{}')
            user_correction = json.loads(row['user_correction'] or '{}')
        except Exception:
            continue
        candidates = ai_prediction.get('candidates') or []
        candidate_labels = [
            ' '.join(str(c.get('label') or '').split())[:80]
            for c in candidates
            if isinstance(c, dict) and c.get('label')
        ][:5]
        correct_label = ' '.join(str(
            user_correction.get('diagnosis')
            or user_correction.get('organ')
            or user_correction.get('feedbackType')
            or ''
        ).split())[:120]
        memo = ' / '.join(
            ' '.join(str(x).split())
            for x in [
                user_correction.get('organ'),
                user_correction.get('diagnosis'),
                user_correction.get('comment'),
                user_correction.get('explanationMemo'),
                user_correction.get('certainty'),
                user_correction.get('feedbackType'),
            ]
            if ' '.join(str(x or '').split())
        )[:500]
        category = ' '.join(str(ai_prediction.get('category') or ai_prediction.get('domain') or '').split())[:80]
        confidence = float(ai_prediction.get('confidence') or 0)
        feedback_type = ' '.join(str(user_correction.get('feedbackType') or '').split())[:120]
        conn.execute(
            '''
            UPDATE vision_quiz_corrections
            SET ai_candidates=?, correct_label=?, memo=?, category=?, confidence=?, feedback_type=?
            WHERE id=?
            ''',
            (
                json.dumps(candidate_labels, ensure_ascii=False),
                correct_label,
                memo,
                category,
                confidence,
                feedback_type,
                row['id'],
            ),
        )
        label = correct_label or feedback_type or '修正メモ'
        summary_category = category or '未分類'
        memory_key = f'{summary_category}:{label}'.lower()
        summary = ' / '.join(x for x in [summary_category, label, memo] if x)[:500]
        conn.execute(
            '''
            INSERT INTO vision_quiz_memory_summaries(memory_key, category, correct_label, summary, count, last_seen_at)
            VALUES(?, ?, ?, ?, 1, datetime('now','localtime'))
            ON CONFLICT(memory_key) DO UPDATE SET
              summary=excluded.summary,
              count=count + 1,
              last_seen_at=excluded.last_seen_at
            ''',
            (memory_key, summary_category, label, summary),
        )
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

VISION_QUIZ_PROMPT = """画像に写っている対象を観察し、断定せずに候補・自信度・確認質問を中心にした学習用クイズを3問生成してください。

重要:
- AIは間違える前提で、ユーザーがAIを育てる体験にします。
- 「これは○○です」と断定せず、「○○の可能性が高そうです」「この画像だけでは断定できません」のように書きます。
- 医療・病理・生物・地理など専門画像では、画像だけで診断名や同定を断定しません。
- クイズは第1候補だけに依存せず、候補と不確実性を反映してください。

出力は以下のJSONのみ（前後の説明・コードブロック不要）:
{
  "title":"断定しない短い推定タイトル",
  "category":"病理/植物/昆虫/空/建物/地形/食べ物/美術/看板/その他",
  "description":"画像所見と不確実性を含む80字程度の説明",
  "confidence":0.0〜1.0,
  "candidates":[
    {"label":"候補名","confidence":0.0〜1.0,"reason":"その候補を考える理由。弱い根拠や限界も書く"}
  ],
  "aiQuestionToUser":"この画像は何だと思いますか？必要ならAIの推定を修正してください。",
  "uncertaintyNote":"スマホ撮影画像・低倍率・切り出し画像では断定を避け、候補として提示します。",
  "quiz":[
    {"question":"候補や鑑別、不確実性に基づく問い","answer":"模範解答（100字以内）"}
  ]
}

ルール:
- candidatesは1〜3件。confidenceは0〜1の数値。
- quizは3問。悪い例:「これは○○です。特徴を答えなさい」。良い例:「この候補を疑う場合、どの所見に注目しますか？」
- 画像が不鮮明・対象不明・不適切な内容の場合も、titleは「判別困難」、confidenceは0、candidatesとquizは空配列にする。"""

# ── Deck API ──────────────────────────────────────────────────
@app.route('/api/decks', methods=['GET'])
def get_decks():
    user_id = _user_id()
    conn = get_db()
    rows = conn.execute('''
        SELECT d.*, COUNT(q.id) as q_count FROM decks d
        LEFT JOIN questions q ON d.id=q.deck_id
        WHERE d.owner_id IS NULL OR d.owner_id=?
        GROUP BY d.id ORDER BY d.created_at DESC
    ''', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/decks', methods=['POST'])
def create_deck():
    d = request.get_json(force=True)
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'デッキ名は必須です'}), 400
    user_id = _user_id()
    conn = get_db()
    cur = conn.execute('INSERT INTO decks(name,description,category,owner_id,created_at) VALUES(?,?,?,?,?)',
        (name, d.get('description',''), d.get('category',''), user_id, datetime.now().strftime('%Y-%m-%d %H:%M')))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/decks/<int:deck_id>', methods=['DELETE'])
def delete_deck(deck_id):
    user_id = _user_id()
    conn = get_db()
    deck = conn.execute('SELECT owner_id FROM decks WHERE id=?', (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return jsonify({'error': 'deck not found'}), 404
    if deck['owner_id'] and deck['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    conn.execute('DELETE FROM questions WHERE deck_id=?', (deck_id,))
    conn.execute('DELETE FROM decks WHERE id=?', (deck_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/decks/claim-public', methods=['POST'])
def claim_public_decks():
    denied = _require_admin()
    if denied: return denied
    user_id = _user_id()
    conn = get_db()
    nd = conn.execute('UPDATE decks SET owner_id=? WHERE owner_id IS NULL', (user_id,)).rowcount
    nq = conn.execute('UPDATE questions SET owner_id=? WHERE owner_id IS NULL', (user_id,)).rowcount
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'decks': nd, 'questions': nq})

# ── Question API ──────────────────────────────────────────────
@app.route('/api/decks/<int:deck_id>/questions', methods=['GET'])
def get_questions(deck_id):
    user_id = _user_id()
    conn = get_db()
    deck = conn.execute('SELECT owner_id FROM decks WHERE id=?', (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return jsonify({'error': 'deck not found'}), 404
    if deck['owner_id'] and deck['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    category = request.args.get('category', '')
    limit    = request.args.get('limit', type=int)
    shuffle  = request.args.get('shuffle', '0') == '1'
    mode     = request.args.get('mode', '')   # 'weak' | 'new' | ''

    difficulty = (request.args.get('difficulty') or '').strip()
    mcq_cc_raw = (request.args.get('mcq_correct_count') or '').strip()
    cat_clause  = ' AND q.category=?' if category else ''
    cat_params  = [category]              if category else []
    # 標準 を指定された場合は difficulty 未設定（NULL）の既存問題もマッチさせる
    if difficulty == '標準':
        diff_clause = ' AND (q.difficulty=? OR q.difficulty IS NULL)'
        diff_params = [difficulty]
    elif difficulty:
        diff_clause = ' AND q.difficulty=?'
        diff_params = [difficulty]
    else:
        diff_clause = ''
        diff_params = []
    if mcq_cc_raw in ('1', '2'):
        mcq_clause = ' AND q.mcq_correct_count=?'
        mcq_params = [int(mcq_cc_raw)]
    else:
        mcq_clause = ''
        mcq_params = []

    if mode == 'weak':
        # Attempted questions, lowest avg score first
        sql  = f'''SELECT q.* FROM questions q
                   JOIN attempts a ON a.question_id = q.id
                   WHERE q.deck_id=? AND a.user_id=? {cat_clause}{diff_clause}{mcq_clause}
                   GROUP BY q.id
                   ORDER BY AVG(a.score) ASC'''
        rows = conn.execute(sql, [deck_id, user_id] + cat_params + diff_params + mcq_params).fetchall()
    elif mode == 'new':
        # Questions with zero attempts
        sql  = f'''SELECT q.* FROM questions q
                   WHERE q.deck_id=?
                   AND q.id NOT IN (SELECT DISTINCT question_id FROM attempts WHERE user_id=?)
                   {cat_clause}{diff_clause}{mcq_clause}'''
        rows = conn.execute(sql, [deck_id, user_id] + cat_params + diff_params + mcq_params).fetchall()
        if shuffle:
            import random; random.shuffle(rows := list(rows))
    else:
        cat_sql = ' AND category=?' if category else ''
        if difficulty == '標準':
            diff_sql = ' AND (difficulty=? OR difficulty IS NULL)'
        elif difficulty:
            diff_sql = ' AND difficulty=?'
        else:
            diff_sql = ''
        mcq_sql = ' AND mcq_correct_count=?' if mcq_cc_raw in ('1', '2') else ''
        order   = ' ORDER BY RANDOM()' if shuffle else ' ORDER BY id ASC'
        sql  = f'SELECT * FROM questions WHERE deck_id=? {cat_sql}{diff_sql}{mcq_sql}{order}'
        rows = conn.execute(sql, [deck_id] + cat_params + diff_params + mcq_params).fetchall()

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
    user_id = _user_id()
    d = request.get_json(force=True)
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': '問題文は必須です'}), 400
    conn = get_db()
    deck = conn.execute('SELECT owner_id FROM decks WHERE id=?', (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return jsonify({'error': 'deck not found'}), 404
    if deck['owner_id'] and deck['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    owner = deck['owner_id'] or user_id
    mcq_options = d.get('mcq_options')  # pre-cached options from push script
    difficulty = (d.get('difficulty') or '').strip() or None
    mcq_correct_count = _coerce_mcq_correct_count(d.get('mcq_correct_count'))
    cur = conn.execute(
        'INSERT INTO questions(deck_id,category,question,model_answer,key_points,guideline_ref,flowchart,mcq_options,owner_id,difficulty,mcq_correct_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (deck_id, (d.get('category') or '').strip(), question,
         d.get('model_answer', ''),
         json.dumps(d.get('key_points', []), ensure_ascii=False),
         d.get('guideline_ref', ''), d.get('flowchart', ''),
         json.dumps(mcq_options) if mcq_options else None,
         owner,
         difficulty,
         mcq_correct_count,
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
    user_id = _user_id()
    d = request.get_json(force=True)
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': '問題文は必須です'}), 400
    conn = get_db()
    row = conn.execute('SELECT owner_id FROM questions WHERE id=?', (q_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'question not found'}), 404
    if row['owner_id'] and row['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    difficulty = (d.get('difficulty') or '').strip() or None
    mcq_correct_count = _coerce_mcq_correct_count(d.get('mcq_correct_count'))
    conn.execute(
        'UPDATE questions SET category=?,question=?,model_answer=?,key_points=?,guideline_ref=?,flowchart=?,difficulty=?,mcq_correct_count=? WHERE id=?',
        ((d.get('category') or '').strip(), question,
         d.get('model_answer', ''),
         json.dumps(d.get('key_points', []), ensure_ascii=False),
         d.get('guideline_ref', ''), d.get('flowchart', ''),
         difficulty, mcq_correct_count, q_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/questions/<int:q_id>', methods=['DELETE'])
def delete_question(q_id):
    user_id = _user_id()
    conn = get_db()
    row = conn.execute('SELECT owner_id FROM questions WHERE id=?', (q_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'question not found'}), 404
    if row['owner_id'] and row['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    conn.execute('DELETE FROM questions WHERE id=?', (q_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Scoring API ───────────────────────────────────────────────
@app.route('/api/score', methods=['POST'])
def score():
    d = request.get_json(force=True)
    user_id = _user_id()
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
            _save_attempt(qid, data, answer, user_id)
            yield f"data: {json.dumps({'done': True, **data}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/api/score-sync', methods=['POST'])
def score_sync():
    d = request.get_json(force=True)
    user_id = _user_id()
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
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        return _daily_limit_response(used, day_limit)

    conn = get_db()
    row = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '問題が見つかりません'}), 404

    kps = json.loads(row['key_points'] or '[]')
    kp_str = '\n'.join(f"- (w={k['w']}) {k['t']}" for k in kps)

    _consume_daily_ai(user_id)
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
        _ensure_wallet(conn2, user_id)
        conn2.execute('UPDATE wallet SET balance = balance + ? WHERE user_id=?', (earned, user_id))
        conn2.commit()
        conn2.close()
    _save_attempt(qid, result, answer, user_id)
    return jsonify(result)

def _save_attempt(qid, d, answer, user_id=None, question_row=None):
    user_id = user_id or _user_id()
    conn = get_db()
    score = d.get('score', 0)
    cur = conn.execute(
        'INSERT INTO attempts(question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,attempted_at,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (qid, score, d.get('grade',''),
         answer, d.get('model_answer',''),
         json.dumps(d.get('covered',[]), ensure_ascii=False),
         json.dumps(d.get('partial',[]), ensure_ascii=False),
         json.dumps(d.get('missed',[]),  ensure_ascii=False),
         d.get('feedback',''), d.get('advice',''),
         datetime.now().strftime('%Y-%m-%d %H:%M'), user_id))
    attempt_id = cur.lastrowid
    conn.execute('''INSERT INTO results(user_id,question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,saved_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id, question_id) DO UPDATE SET
            score=excluded.score, grade=excluded.grade, answer=excluded.answer,
            model_answer=excluded.model_answer, covered=excluded.covered,
            partial=excluded.partial, missed=excluded.missed,
            feedback=excluded.feedback, advice=excluded.advice, saved_at=excluded.saved_at''',
        (user_id, qid, score, d.get('grade',''),
         answer, d.get('model_answer',''),
         json.dumps(d.get('covered',[]), ensure_ascii=False),
         json.dumps(d.get('partial',[]),  ensure_ascii=False),
         json.dumps(d.get('missed',[]),   ensure_ascii=False),
         d.get('feedback',''), d.get('advice',''),
         datetime.now().strftime('%Y-%m-%d %H:%M')))
    if question_row is None:
        try:
            question_row = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
        except Exception:
            question_row = None
    if question_row is not None:
        try:
            _record_attempt_graph(conn, qid=qid, attempt_id=attempt_id, question_row=question_row,
                                  result=d, answer=answer, user_id=user_id)
        except Exception as e:
            print('[knowledge] attempt graph failed:', e)
    conn.commit()
    conn.close()


# ── Knowledge Graph helpers ───────────────────────────────────
def _now_iso():
    return datetime.now().isoformat(timespec='seconds')

def _kg_meta(value):
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

def _save_kg_node(conn, *, node_id, user_id, type_, label, summary='', body='', source_id='', metadata=None):
    now = _now_iso()
    existing = conn.execute('SELECT created_at FROM knowledge_nodes WHERE id=?', (node_id,)).fetchone()
    created = existing['created_at'] if existing else now
    conn.execute("""
        INSERT INTO knowledge_nodes(id,user_id,type,label,summary,body,source_id,metadata,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, summary=excluded.summary, body=excluded.body,
            source_id=excluded.source_id, metadata=excluded.metadata, updated_at=excluded.updated_at
    """, (node_id, user_id, type_, (label or type_)[:180], (summary or '')[:500], body or '', source_id or '',
          _kg_meta(metadata), created, now))
    return node_id

def _save_kg_edge(conn, *, user_id, type_, source, target, metadata=None):
    edge_id = f"{type_}:{source}:{target}"
    now = _now_iso()
    conn.execute("""
        INSERT OR IGNORE INTO knowledge_edges(id,user_id,type,source,target,metadata,created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (edge_id, user_id, type_, source, target, _kg_meta(metadata), now))
    return edge_id

def _kg_extract_candidates(text):
    clean = re.sub(r'\s+', ' ', (text or '').strip())
    if not clean:
        return []
    hits = []
    patterns = [
        r'\b[A-Z]{2,}(?:\s?\d{1,3}(?:-\d{1,3})?)?\b',
        r'\b[A-Z]{1,4}\.\d{1,3}\b',
    ]
    for pat in patterns:
        for m in re.finditer(pat, clean):
            val = m.group(0).strip()
            if val and val not in hits:
                hits.append(val)
    return hits[:6]

def _kg_question_key_points(question_row):
    try:
        kps = json.loads(question_row['key_points'] or '[]')
    except Exception:
        return []
    points = []
    for item in kps:
        if isinstance(item, dict) and item.get('t'):
            text = re.sub(r'\s+', ' ', str(item['t']).strip())
            if text:
                points.append(text)
    return points

def _build_concept_card(question_row, label):
    question_text = question_row['question'] or ''
    model_answer = question_row['model_answer'] or ''
    guideline_ref = question_row['guideline_ref'] or ''
    points = _kg_question_key_points(question_row)
    drug_hits = _kg_extract_candidates(question_text + '\n' + model_answer)
    trial_hits = _kg_extract_candidates(guideline_ref + '\n' + model_answer)
    summary = question_row['category'] or label or '関連知識'
    lines = []
    if question_text:
        lines.append(f'問題: {question_text}')
    if drug_hits:
        lines.append('薬剤・レジメン候補: ' + ' / '.join(drug_hits))
    if points:
        lines.append('要点: ' + ' / '.join(points[:3]))
    if guideline_ref:
        lines.append('根拠: ' + guideline_ref)
    if trial_hits:
        lines.append('試験・略称候補: ' + ' / '.join(trial_hits))
    body = '\n'.join(lines)
    metadata = {
        'category': question_row['category'] or '',
        'questionId': question_row['id'],
        'question': question_text[:260],
        'guidelineRef': guideline_ref,
        'drugCandidates': drug_hits,
        'trialCandidates': trial_hits,
    }
    return summary, body, metadata

def _concept_node(conn, user_id, label, *, source_id='', metadata=None, summary='関連知識', body=''):
    clean = re.sub(r'\s+', ' ', (label or '').strip())
    if not clean:
        return None
    slug = re.sub(r'[^0-9A-Za-zぁ-んァ-ン一-龥ー]+', '-', clean).strip('-').lower()[:80]
    if not slug:
        import uuid as _uuid
        slug = _uuid.uuid4().hex[:8]
    node_id = f"concept:{user_id}:{slug}"
    return _save_kg_node(conn, node_id=node_id, user_id=user_id, type_='concept',
                         label=clean, summary=summary, body=body, source_id=source_id, metadata=metadata or {})

def _record_attempt_graph(conn, *, qid, attempt_id, question_row, result, answer, user_id):
    q_node = f"question:{qid}"
    answer_node = f"answer:user:{attempt_id}"
    agent_node = 'agent:claude'
    question_text = question_row['question'] or ''
    model_answer = result.get('model_answer') or question_row['model_answer'] or ''
    category = question_row['category'] or ''
    advice = result.get('advice') or ''
    feedback = result.get('feedback') or ''

    combined_body_parts = []
    if answer:
        combined_body_parts.append(f"【あなたの回答】\n{answer}")
    if model_answer:
        combined_body_parts.append(f"【模範解答】\n{model_answer}")
    if advice:
        combined_body_parts.append(f"【AIアドバイス】\n{advice}")
    combined_body = '\n\n'.join(combined_body_parts)

    _save_kg_node(conn, node_id=q_node, user_id=user_id, type_='question',
                  label=question_text[:80] or f'問題 {qid}', summary=category,
                  body=question_text, source_id=str(qid), metadata={'questionId': qid, 'category': category})
    is_mcq = answer in ('5択:正解', '5択:不正解')
    if is_mcq:
        answer_label = '回答 ○' if answer == '5択:正解' else '回答 ×'
    else:
        answer_label = f"回答 {result.get('grade','')}".strip()
    _save_kg_node(conn, node_id=answer_node, user_id=user_id, type_='answer',
                  label=answer_label, summary=feedback,
                  body=combined_body, source_id=str(attempt_id),
                  metadata={'questionId': qid, 'attemptId': attempt_id, 'score': result.get('score'), 'grade': result.get('grade'), 'agent': 'claude', 'isMcq': is_mcq})
    _save_kg_node(conn, node_id=agent_node, user_id=user_id, type_='agent',
                  label='Claude', summary='採点・解説エージェント', metadata={'provider': 'anthropic'})

    _save_kg_edge(conn, user_id=user_id, type_='generated_from', source=answer_node, target=q_node)
    _save_kg_edge(conn, user_id=user_id, type_='explains', source=agent_node, target=answer_node)

    concept_labels = [category]
    concept_labels += result.get('covered') or []
    concept_labels += result.get('partial') or []
    concept_labels += result.get('missed') or []
    seen = set()
    for label in concept_labels:
        if not label or label in seen:
            continue
        seen.add(label)
        summary, body, meta = _build_concept_card(question_row, label)
        meta.update({'questionId': qid, 'conceptLabel': label, 'attemptId': attempt_id})
        concept = _concept_node(conn, user_id, label, source_id=str(qid), metadata=meta, summary=summary, body=body)
        if not concept:
            continue
        _save_kg_edge(conn, user_id=user_id, type_='relates_to', source=q_node, target=concept)
        if label in (result.get('missed') or []):
            _save_kg_edge(conn, user_id=user_id, type_='confused_with', source=answer_node, target=concept)
        else:
            _save_kg_edge(conn, user_id=user_id, type_='explains', source=answer_node, target=concept)

def _backfill_knowledge_graph_for_user(user_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT a.*, q.id AS q_id, q.deck_id, q.category, q.question, q.model_answer AS q_model_answer,
               q.key_points, q.guideline_ref, q.flowchart, q.created_at AS q_created_at
        FROM attempts a
        JOIN questions q ON q.id = a.question_id
        WHERE a.user_id=?
        ORDER BY a.id ASC
    """, (user_id,)).fetchall()
    made = 0
    for r in rows:
        qrow = {
            'id': r['q_id'],
            'deck_id': r['deck_id'],
            'category': r['category'],
            'question': r['question'],
            'model_answer': r['q_model_answer'],
            'key_points': r['key_points'],
            'guideline_ref': r['guideline_ref'],
            'flowchart': r['flowchart'],
            'created_at': r['q_created_at'],
        }
        result = {
            'score': r['score'],
            'grade': r['grade'],
            'model_answer': r['model_answer'] or r['q_model_answer'] or '',
            'covered': json.loads(r['covered'] or '[]'),
            'partial': json.loads(r['partial'] or '[]'),
            'missed': json.loads(r['missed'] or '[]'),
            'feedback': r['feedback'] or '',
            'advice': r['advice'] or '',
        }
        try:
            _record_attempt_graph(conn, qid=r['question_id'], attempt_id=r['id'], question_row=qrow,
                                  result=result, answer=r['answer'] or '', user_id=user_id)
            made += 1
        except Exception as e:
            print('[knowledge] backfill row failed:', e)
    conn.commit()
    conn.close()
    return made

def _row_to_kg_node(r):
    return {
        'id': r['id'],
        'userId': r['user_id'] or '',
        'type': r['type'],
        'label': r['label'],
        'summary': r['summary'] or '',
        'body': r['body'] or '',
        'sourceId': r['source_id'] or '',
        'metadata': json.loads(r['metadata'] or '{}'),
        'createdAt': r['created_at'] or '',
        'updatedAt': r['updated_at'] or '',
    }

def _row_to_kg_edge(r):
    return {
        'id': r['id'],
        'userId': r['user_id'] or '',
        'type': r['type'],
        'source': r['source'],
        'target': r['target'],
        'metadata': json.loads(r['metadata'] or '{}'),
        'createdAt': r['created_at'] or '',
    }

@app.route('/api/knowledge-graph/backfill', methods=['POST'])
def backfill_knowledge_graph():
    made = _backfill_knowledge_graph_for_user(_user_id())
    return jsonify({'ok': True, 'attempts': made})

@app.route('/api/knowledge-graph')
def get_knowledge_graph():
    user_id = _user_id()
    try:
        limit = max(20, min(int(request.args.get('limit', 240)), 500))
    except Exception:
        limit = 240
    deck_id = (request.args.get('deck_id') or '').strip()
    expand_bridges = (request.args.get('expand') or '').strip() == '1'
    bridges_info = {'count': 0, 'concept_ids': [], 'other_deck_names': []}

    def load_graph(uid):
        conn = get_db()
        if deck_id:
            q_rows = conn.execute('SELECT id FROM questions WHERE deck_id=?', (deck_id,)).fetchall()
            deck_qids = {r['id'] for r in q_rows}
            q_node_ids = {f"question:{qid}" for qid in deck_qids}
            if not q_node_ids:
                conn.close()
                return [], []
            ph_q = ','.join(['?'] * len(q_node_ids))
            seed_edges = conn.execute(
                f"SELECT source, target FROM knowledge_edges WHERE user_id=? AND (source IN ({ph_q}) OR target IN ({ph_q}))",
                [uid, *q_node_ids, *q_node_ids]
            ).fetchall()
            related_ids = set(q_node_ids)
            for e in seed_edges:
                related_ids.add(e['source'])
                related_ids.add(e['target'])
            related_ids.add('agent:claude')

            in_deck_concepts = {nid for nid in related_ids if nid.startswith('concept:')}
            bridge_external_q_ids = set()
            bridge_concept_ids = set()
            if in_deck_concepts:
                ph_c = ','.join(['?'] * len(in_deck_concepts))
                bridge_edges = conn.execute(
                    f"SELECT source, target FROM knowledge_edges WHERE user_id=? AND (source IN ({ph_c}) OR target IN ({ph_c}))",
                    [uid, *in_deck_concepts, *in_deck_concepts]
                ).fetchall()
                for e in bridge_edges:
                    for endpoint in (e['source'], e['target']):
                        if endpoint.startswith('question:') and endpoint not in q_node_ids:
                            bridge_external_q_ids.add(endpoint)
                            for c in (e['source'], e['target']):
                                if c in in_deck_concepts:
                                    bridge_concept_ids.add(c)
            bridges_info['count'] = len(bridge_external_q_ids)
            bridges_info['concept_ids'] = sorted(bridge_concept_ids)
            if bridge_external_q_ids:
                ext_qids = [int(x.split(':', 1)[1]) for x in bridge_external_q_ids if x.split(':', 1)[1].isdigit()]
                if ext_qids:
                    ph_ext = ','.join(['?'] * len(ext_qids))
                    name_rows = conn.execute(
                        f"SELECT DISTINCT d.name FROM decks d JOIN questions q ON q.deck_id=d.id WHERE q.id IN ({ph_ext})",
                        ext_qids
                    ).fetchall()
                    bridges_info['other_deck_names'] = [r['name'] for r in name_rows]

            if expand_bridges and bridge_external_q_ids:
                related_ids |= bridge_external_q_ids
                ph_b = ','.join(['?'] * len(bridge_external_q_ids))
                more_edges = conn.execute(
                    f"SELECT source, target FROM knowledge_edges WHERE user_id=? AND (source IN ({ph_b}) OR target IN ({ph_b}))",
                    [uid, *bridge_external_q_ids, *bridge_external_q_ids]
                ).fetchall()
                for e in more_edges:
                    related_ids.add(e['source'])
                    related_ids.add(e['target'])

            ph_n = ','.join(['?'] * len(related_ids))
            nodes = conn.execute(
                f"SELECT * FROM knowledge_nodes WHERE (user_id=? OR type='agent') AND id IN ({ph_n}) ORDER BY updated_at DESC LIMIT ?",
                [uid, *related_ids, limit]
            ).fetchall()
            final_ids = [r['id'] for r in nodes]
            edges = []
            if final_ids:
                ph_f = ','.join(['?'] * len(final_ids))
                edges = conn.execute(
                    f"SELECT * FROM knowledge_edges WHERE user_id=? AND source IN ({ph_f}) AND target IN ({ph_f}) ORDER BY created_at DESC LIMIT ?",
                    [uid, *final_ids, *final_ids, limit * 2]
                ).fetchall()
            conn.close()
            return nodes, edges
        nodes = conn.execute("""
            SELECT * FROM knowledge_nodes
            WHERE user_id=? OR type='agent'
            ORDER BY updated_at DESC
            LIMIT ?
        """, (uid, limit)).fetchall()
        node_ids = [r['id'] for r in nodes]
        edges = []
        if node_ids:
            ph = ','.join(['?'] * len(node_ids))
            edges = conn.execute(f"""
                SELECT * FROM knowledge_edges
                WHERE user_id=? AND source IN ({ph}) AND target IN ({ph})
                ORDER BY created_at DESC
                LIMIT ?
            """, [uid, *node_ids, *node_ids, limit * 2]).fetchall()
        conn.close()
        return nodes, edges

    def has_learning_nodes(rows):
        return any(r['type'] in ('question', 'image', 'answer', 'correction') for r in rows)

    try:
        nodes, edges = load_graph(user_id)
        if not has_learning_nodes(nodes):
            _backfill_knowledge_graph_for_user(user_id)
            nodes, edges = load_graph(user_id)
        return jsonify({
            'nodes': [_row_to_kg_node(r) for r in nodes],
            'edges': [_row_to_kg_edge(r) for r in edges],
            'bridges': bridges_info if deck_id else None,
        })
    except Exception as e:
        print('[knowledge] get_knowledge_graph failed:', e)
        return jsonify({'ok': False, 'error': 'knowledge_graph_failed', 'message': str(e), 'nodes': [], 'edges': [], 'bridges': None}), 500

@app.route('/api/knowledge-graph/correction', methods=['POST'])
def create_knowledge_correction():
    import uuid as _uuid
    d = request.get_json(force=True)
    qid = d.get('question_id')
    text = (d.get('correction') or '').strip()
    if not qid or not text:
        return jsonify({'error': 'question_id and correction are required'}), 400
    user_id = _user_id()
    conn = get_db()
    q = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    if not q:
        conn.close()
        return jsonify({'error': '問題が見つかりません'}), 404
    node_id = f"correction:{_uuid.uuid4().hex}"
    q_node = f"question:{qid}"
    summary, body, meta = _build_concept_card(q, q['category'] or '')
    _save_kg_node(conn, node_id=q_node, user_id=user_id, type_='question',
                  label=(q['question'] or '')[:80] or f'問題 {qid}', summary=q['category'] or '',
                  body=q['question'] or '', source_id=str(qid),
                  metadata={'questionId': qid, 'category': q['category'] or '', 'summaryHint': summary, 'bodyHint': body[:500], **meta})
    _save_kg_node(conn, node_id=node_id, user_id=user_id, type_='correction',
                  label='ユーザー修正', summary=text[:120], body=text, source_id=str(qid),
                  metadata={'questionId': qid, 'grade': d.get('grade'), 'score': d.get('score')})
    _save_kg_edge(conn, user_id=user_id, type_='corrected_by', source=q_node, target=node_id)
    _save_kg_edge(conn, user_id=user_id, type_='relates_to', source=node_id, target=q_node)
    concepts = d.get('concepts') or []
    if isinstance(concepts, str):
        concepts = [concepts]
    base_labels = [q['category'] or ''] + [c for c in concepts if isinstance(c, str)]
    for label in base_labels:
        meta_iter = dict(meta)
        meta_iter.update({'questionId': qid, 'conceptLabel': label})
        concept = _concept_node(conn, user_id, label, source_id=str(qid), metadata=meta_iter, summary=summary, body=body)
        if concept:
            _save_kg_edge(conn, user_id=user_id, type_='relates_to', source=node_id, target=concept)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'node': {'id': node_id, 'type': 'correction', 'label': 'ユーザー修正'}})

# ── Results / Attempts API ────────────────────────────────────
@app.route('/api/results')
def get_results():
    user_id = _user_id()
    conn = get_db()
    rows = conn.execute('SELECT * FROM results WHERE user_id=?', (user_id,)).fetchall()
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

@app.route('/api/attempts', methods=['POST'])
def post_attempt_manual():
    """シンプル採点モード用: AI不使用で attempt を記録する。daily_ai 消費なし。"""
    user_id = _user_id()
    d = request.get_json(force=True) or {}
    question_id = int(d.get('question_id') or 0)
    if not question_id:
        return jsonify({'error': 'question_id required'}), 400
    score = int(d.get('score') or 0)
    grade = str(d.get('grade') or 'D')[:1]
    answer = str(d.get('answer') or '')
    feedback = str(d.get('feedback') or '')
    covered = d.get('covered') or []
    partial = d.get('partial') or []
    missed  = d.get('missed')  or []
    advice  = str(d.get('advice') or '')
    conn = get_db()
    qrow = conn.execute('SELECT model_answer FROM questions WHERE id=?', (question_id,)).fetchone()
    if not qrow:
        conn.close()
        return jsonify({'error': 'question not found'}), 404
    conn.execute(
        'INSERT INTO attempts(question_id,score,grade,answer,model_answer,covered,partial,missed,feedback,advice,attempted_at,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (question_id, score, grade, answer, qrow['model_answer'] or '',
         json.dumps(covered, ensure_ascii=False), json.dumps(partial, ensure_ascii=False), json.dumps(missed, ensure_ascii=False),
         feedback, advice, datetime.now().isoformat(timespec='seconds'), user_id),
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/attempts')
def get_attempts():
    user_id = _user_id()
    conn = get_db()
    rows = conn.execute(
        'SELECT question_id,score,grade,answer,covered,partial,missed,feedback,advice,attempted_at FROM attempts WHERE user_id=? ORDER BY attempted_at ASC',
        (user_id,)
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
    trial = _check_trial(_user_id())
    if trial['status'] == 'expired':
        return _trial_expired_response()
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
def _clean_memory_text(value, limit=180):
    return ' '.join(str(value or '').split())[:limit]

def _extract_vision_memory_fields(ai_prediction, user_correction):
    candidates = ai_prediction.get('candidates') or []
    candidate_labels = [
        _clean_memory_text(c.get('label'), 80)
        for c in candidates
        if isinstance(c, dict) and c.get('label')
    ]
    correct_label = (
        user_correction.get('diagnosis')
        or user_correction.get('organ')
        or user_correction.get('feedbackType')
        or ''
    )
    memo = ' / '.join(x for x in [
        user_correction.get('organ'),
        user_correction.get('diagnosis'),
        user_correction.get('comment'),
        user_correction.get('explanationMemo'),
        user_correction.get('certainty'),
        user_correction.get('feedbackType'),
    ] if _clean_memory_text(x))
    return {
        'ai_candidates': json.dumps(candidate_labels[:5], ensure_ascii=False),
        'correct_label': _clean_memory_text(correct_label, 120),
        'memo': _clean_memory_text(memo, 500),
        'category': _clean_memory_text(ai_prediction.get('category') or ai_prediction.get('domain'), 80),
        'confidence': float(ai_prediction.get('confidence') or 0),
        'feedback_type': _clean_memory_text(user_correction.get('feedbackType'), 120),
    }

def _memory_terms_from_prediction(prediction):
    terms = []
    for key in ['category', 'domain', 'title', 'subject']:
        value = _clean_memory_text(prediction.get(key), 80)
        if value:
            terms.append(value)
    for c in prediction.get('candidates') or []:
        if isinstance(c, dict):
            label = _clean_memory_text(c.get('label'), 80)
            if label:
                terms.append(label)
    return [t for i, t in enumerate(terms) if t and t not in terms[:i]]

def _score_memory_row(row, terms, category):
    keys = set(row.keys())
    def val(name):
        return row[name] if name in keys else ''
    haystack = ' '.join([
        val('category') or '',
        val('correct_label') or '',
        val('memo') or '',
        val('ai_candidates') or '',
        val('feedback_type') or '',
        val('summary') or '',
    ]).lower()
    score = 0
    if category and category == (val('category') or ''):
        score += 2
    ai_candidates = (val('ai_candidates') or '').lower()
    for term in terms:
        t = term.lower()
        if not t:
            continue
        correct_label = (val('correct_label') or '').lower()
        if t == correct_label:
            score += 10
        elif correct_label and (t in correct_label or correct_label in t):
            score += 6
        elif t in ai_candidates:
            score += 6
        elif t in haystack:
            score += 3
    return score

def _upsert_vision_memory_summary(conn, fields, created_at):
    label = fields['correct_label'] or fields['feedback_type'] or '修正メモ'
    category = fields['category'] or '未分類'
    memory_key = f'{category}:{label}'.lower()
    summary_parts = [category, label, fields['memo']]
    summary = _clean_memory_text(' / '.join(x for x in summary_parts if x), 500)
    conn.execute(
        '''
        INSERT INTO vision_quiz_memory_summaries(memory_key, category, correct_label, summary, count, last_seen_at)
        VALUES(?, ?, ?, ?, 1, ?)
        ON CONFLICT(memory_key) DO UPDATE SET
          summary=excluded.summary,
          count=count + 1,
          last_seen_at=excluded.last_seen_at
        ''',
        (memory_key, category, label, summary, created_at),
    )

def _vision_quiz_memory_prompt(prediction):
    terms = _memory_terms_from_prediction(prediction)
    category = _clean_memory_text(prediction.get('category') or prediction.get('domain'), 80)
    if not terms and not category:
        return ''
    conn = get_db()
    rows = conn.execute(
        '''
        SELECT id, image_id, ai_candidates, correct_label, memo, category, confidence, feedback_type, created_at
        FROM vision_quiz_corrections
        ORDER BY id DESC
        LIMIT 1000
        '''
    ).fetchall()
    summary_rows = conn.execute(
        '''
        SELECT category, correct_label, summary, count, last_seen_at
        FROM vision_quiz_memory_summaries
        ORDER BY count DESC, last_seen_at DESC
        LIMIT 100
        '''
    ).fetchall()
    conn.close()

    scored = []
    for row in rows:
        score = _score_memory_row(row, terms, category)
        if score >= 6:
            scored.append((score, row))
    scored.sort(key=lambda x: (x[0], x[1]['id']), reverse=True)
    selected = [row for _, row in scored[:20]]

    scored_summaries = []
    for row in summary_rows:
        score = _score_memory_row(row, terms, category)
        if score >= 6:
            scored_summaries.append((score + min(int(row['count'] or 0), 10), row))
    scored_summaries.sort(key=lambda x: (x[0], x[1]['count'] or 0), reverse=True)

    lines = []
    for row in [r for _, r in scored_summaries[:8]]:
        lines.append(f"頻出({row['count']}回): {_clean_memory_text(row['summary'], 220)}")
    for row in selected:
        candidates = row['ai_candidates'] or ''
        try:
            parsed_candidates = json.loads(candidates)
            candidates = ', '.join(parsed_candidates[:3]) if isinstance(parsed_candidates, list) else candidates
        except Exception:
            pass
        line = ' / '.join(x for x in [
            row['category'],
            f"AI候補: {candidates}" if candidates else '',
            f"正解/修正: {row['correct_label']}" if row['correct_label'] else '',
            row['memo'],
        ] if x)
        if line:
            lines.append(_clean_memory_text(line, 260))
    if not lines:
        return ''
    return (
        '\n\n過去のユーザーフィードバックから、今回の初回推定に関連しそうな記憶だけを抽出しました。'
        '似ている場合だけ参考にし、矛盾する場合は画像所見を優先してください。'
        'この記憶は候補の出し方・不確実性の扱い・修正候補に反映します:\n- '
        + '\n- '.join(lines[:28])
    )

def _call_vision_quiz(image_bytes, media_type, user_key, memory_prompt=''):
    client = anthropic.Anthropic(api_key=user_key)
    msg = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=2048,
        messages=[{'role':'user','content':[
            {'type':'image','source':{'type':'base64','media_type':media_type,'data':base64.standard_b64encode(image_bytes).decode()}},
            {'type':'text','text':VISION_QUIZ_PROMPT + memory_prompt},
        ]}])
    text = msg.content[0].text.strip()
    m = re.search(r'\{.*\}', text, re.DOTALL)
    return json.loads(m.group() if m else text)

def _normalize_vision_quiz_result(result):
    quiz = result.get('quiz')
    if quiz is None:
        quiz = [
            {'question': q.get('q', ''), 'answer': q.get('a', '')}
            for q in result.get('questions', [])
        ]
    candidates = result.get('candidates') or []
    if not candidates and (result.get('subject') or result.get('summary')):
        candidates = [{
            'label': result.get('subject') or '判別困難',
            'confidence': float(result.get('confidence') or 0),
            'reason': result.get('summary') or '旧形式のAI出力から変換しました',
        }]
    return {
        'title': result.get('title') or result.get('subject') or '判別困難',
        'category': result.get('category') or result.get('domain') or '',
        'description': result.get('description') or result.get('summary') or '',
        'confidence': float(result.get('confidence') or 0),
        'candidates': candidates[:3],
        'aiQuestionToUser': result.get('aiQuestionToUser') or 'この画像は何だと思いますか？必要ならAIの推定を修正してください。',
        'uncertaintyNote': result.get('uncertaintyNote') or 'スマホ撮影画像・低倍率・切り出し画像では断定を避け、候補として提示します。',
        'quiz': quiz,
        # 旧フロント互換
        'subject': result.get('subject') or result.get('title') or '',
        'domain': result.get('domain') or result.get('category') or '',
        'summary': result.get('summary') or result.get('description') or '',
        'questions': result.get('questions') or [
            {'q': q.get('question', ''), 'a': q.get('answer', ''), 'level': ['A', 'B', 'C'][i] if i < 3 else 'B'}
            for i, q in enumerate(quiz)
        ],
    }

def _vision_quiz_fallback():
    return {
        'ok': True,
        'imageId': '',
        'fallback': True,
        'question': {
            'category': '一般',
            'question': '画像から問題を生成できませんでした。別の画像でお試しください。',
            'choices': ['再試行', 'スキップ', '別の写真を撮る', 'キャンセル'],
            'correctIndex': 0,
            'explanation': 'AIが画像を解析できなかったため、フォールバック問題を表示しています。',
        },
    }

@app.route('/api/vision-quiz', methods=['POST'])
def vision_quiz():
    import traceback
    try:
        user_key = ANTHROPIC_API_KEY
        if not user_key:
            return jsonify({'ok': False, 'error': 'api_key_missing', 'message': 'APIキーが未設定です'}), 401
        user_id = _user_id()
        trial = _check_trial(user_id)
        if trial['status'] == 'expired':
            return _trial_expired_response()
        try:
            ok, current, limit = _check_rate('vision_quiz')
        except Exception as e:
            print('[vision-quiz] rate check failed:', e)
            ok, current, limit = True, 0, 0
        if not ok:
            return _rate_limit_response('vision_quiz', current, limit)
        ok_day, used, day_limit = _check_daily_ai(user_id)
        if not ok_day:
            return _daily_limit_response(used, day_limit)
        _consume_daily_ai(user_id)

        image_bytes = None
        mt = 'image/jpeg'
        try:
            f = request.files.get('file')
        except Exception:
            f = None
        if f:
            name = (f.filename or '').lower()
            if not name.endswith(('.jpg','.jpeg','.png','.webp','.gif')):
                return jsonify({'ok': False, 'error': 'invalid_image', 'message': '画像のみ対応（jpg/png/webp/gif）'}), 400
            try:
                image_bytes = f.read()
            except Exception as e:
                return jsonify({'ok': False, 'error': 'read_failed', 'message': '画像の読み取りに失敗', 'detail': str(e)}), 400
            mt = ('image/jpeg' if name.endswith(('.jpg','.jpeg')) else
                  'image/png'  if name.endswith('.png')  else
                  'image/webp' if name.endswith('.webp') else 'image/gif')
        else:
            body = request.get_json(silent=True) or {}
            b64 = body.get('image_b64', '')
            if not b64:
                return jsonify({'ok': False, 'error': 'no_file', 'message': 'ファイルがありません'}), 400
            mt = body.get('mime_type', 'image/jpeg')
            try:
                image_bytes = base64.standard_b64decode(b64)
            except Exception as e:
                return jsonify({'ok': False, 'error': 'b64_decode_failed', 'message': 'base64デコード失敗', 'detail': str(e)}), 400

        if not image_bytes:
            return jsonify({'ok': False, 'error': 'empty_image', 'message': '画像データが空です'}), 400

        try:
            result = _normalize_vision_quiz_result(_call_vision_quiz(image_bytes, mt, user_key))
        except Exception as e:
            print('[vision-quiz] AI call/normalize failed:', e)
            traceback.print_exc()
            fb = _vision_quiz_fallback()
            fb['error'] = 'ai_call_failed'
            fb['message'] = '画像解析中にエラーが発生しました（フォールバック問題を返却）'
            fb['detail'] = str(e)
            return jsonify(fb), 200
        try:
            memory_prompt = _vision_quiz_memory_prompt(result)
            if memory_prompt:
                result = _normalize_vision_quiz_result(_call_vision_quiz(image_bytes, mt, user_key, memory_prompt))
        except Exception as e:
            print('[vision-quiz] memory pass failed (ignored):', e)
        try:
            image_id = hashlib.sha256(image_bytes).hexdigest()[:24]
        except Exception:
            image_id = ''
        return jsonify({'ok': True, 'imageId': image_id, **result})
    except Exception as e:
        print('[vision-quiz] unhandled exception:', e)
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'vision_quiz_failed', 'message': '画像解析中にエラーが発生しました', 'detail': str(e)}), 500

@app.route('/api/vision-quiz/correction', methods=['POST'])
def save_vision_quiz_correction():
    user_id = _user_id()
    d = request.get_json(force=True)
    image_id = (d.get('imageId') or '').strip()
    ai_prediction = d.get('aiPrediction') or {}
    user_correction = d.get('userCorrection') or {}
    if not image_id:
        return jsonify({'ok': False, 'error': 'imageId は必須です'}), 400
    if not user_correction:
        return jsonify({'ok': False, 'error': 'userCorrection は必須です'}), 400
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fields = _extract_vision_memory_fields(ai_prediction, user_correction)
    conn = get_db()
    conn.execute(
        '''
        INSERT INTO vision_quiz_corrections(
            image_id, ai_prediction, user_correction, ai_candidates, correct_label,
            memo, category, confidence, feedback_type, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ''',
        (
            image_id,
            json.dumps(ai_prediction, ensure_ascii=False),
            json.dumps(user_correction, ensure_ascii=False),
            fields['ai_candidates'],
            fields['correct_label'],
            fields['memo'],
            fields['category'],
            fields['confidence'],
            fields['feedback_type'],
            created_at,
        )
    )
    _upsert_vision_memory_summary(conn, fields, created_at)
    conn.commit()
    conn.close()
    organ = user_correction.get('organ') or ''
    diagnosis = user_correction.get('diagnosis') or ''
    label = ''.join([organ, diagnosis]) or '修正内容'
    return jsonify({
        'ok': True,
        'createdAt': created_at,
        'message': f'ありがとうございます。{label}の可能性として記録しました。次回から似た所見を見たときは、この修正も候補に入れます。'
    })

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
    user_id = _user_id()
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
            f'SELECT score,grade FROM attempts WHERE user_id=? AND question_id IN ({ph})', [user_id] + qids).fetchall()
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
    user_id = _user_id()
    conn = get_db()
    _ensure_wallet(conn, user_id)
    conn.commit()
    row = conn.execute('SELECT balance FROM wallet WHERE user_id=?', (user_id,)).fetchone()
    conn.close()
    return jsonify({'balance': row['balance'] if row else 0})

@app.route('/api/wallet/spend', methods=['POST'])
def spend_wallet():
    user_id = _user_id()
    d = request.get_json(force=True)
    cost = int(d.get('cost', 0))
    conn = get_db()
    _ensure_wallet(conn, user_id)
    row = conn.execute('SELECT balance FROM wallet WHERE user_id=?', (user_id,)).fetchone()
    balance = row['balance'] if row else 0
    if balance < cost:
        conn.close()
        return jsonify({'error': '石が足りません'}), 400
    conn.execute('UPDATE wallet SET balance = balance - ? WHERE user_id=?', (cost, user_id))
    conn.commit()
    new_balance = conn.execute('SELECT balance FROM wallet WHERE user_id=?', (user_id,)).fetchone()['balance']
    conn.close()
    return jsonify({'balance': new_balance})

@app.route('/api/wallet/earn', methods=['POST'])
def earn_wallet():
    user_id = _user_id()
    d = request.get_json(force=True)
    amount = int(d.get('amount', 0))
    conn = get_db()
    _ensure_wallet(conn, user_id)
    conn.execute('UPDATE wallet SET balance = balance + ? WHERE user_id=?', (amount, user_id))
    conn.commit()
    new_balance = conn.execute('SELECT balance FROM wallet WHERE user_id=?', (user_id,)).fetchone()['balance']
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
    # クライアントが他人の token を送ってきても無視し、認証済 user_id を採用する
    user_token = _user_id()
    ids = d.get('achievement_ids', [])
    if not ids:
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
    user_id = _user_id()
    conn = get_db()
    row = conn.execute('SELECT key_points, flowchart, category, owner_id FROM questions WHERE id=?', (q_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['owner_id'] and row['owner_id'] != user_id:
        return jsonify({'error': 'forbidden'}), 403
    kps = json.loads(row['key_points'] or '[]')
    hints = [k['t'] for k in kps if isinstance(k, dict) and k.get('t')]
    return jsonify({
        'category': row['category'],
        'flowchart': row['flowchart'] or '',
        'key_count': len(hints),
        'first_hint': hints[0] if hints else '',
    })

# ── MCQ ──────────────────────────────────────────────────────
def _coerce_mcq_correct_count(value):
    if value is None or value == '':
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n in (1, 2) else None

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
    # URLクエリ未指定なら問題ごとの mcq_correct_count を採用する
    if requested_count is None:
        per_question = _coerce_mcq_correct_count(row['mcq_correct_count'])
        if per_question is not None:
            requested_count = per_question
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
    user_id = _user_id()
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        conn.close()
        return _daily_limit_response(used, day_limit)
    import random
    kps = json.loads(row['key_points'] or '[]')
    answer_text = row['model_answer'] or '\n'.join(
        f"・{k['t']}" for k in kps if isinstance(k, dict) and k.get('t'))
    _consume_daily_ai(user_id)
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
        conn.execute('UPDATE questions SET mcq_options=? WHERE id=?', (json.dumps(result, ensure_ascii=False), q_id))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
    conn.close()
    return jsonify(result)

@app.route('/api/mcq/clear-cache', methods=['POST'])
def clear_mcq_cache():
    denied = _require_admin()
    if denied: return denied
    """mcq_options を全てNULLにリセット（択数変更時などに使用）。"""
    conn = get_db()
    conn.execute('UPDATE questions SET mcq_options=NULL')
    conn.commit()
    count = conn.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'cleared': count})

@app.route('/api/mcq/generate-all', methods=['POST'])
def generate_all_mcq():
    denied = _require_admin()
    if denied: return denied
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
    user_id = _user_id()
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
    _save_attempt(q_id, result, '5択:正解' if correct else '5択:不正解', user_id)
    conn2 = get_db()
    _ensure_wallet(conn2, user_id)
    conn2.execute('UPDATE wallet SET balance = balance + ? WHERE user_id=?', (coins, user_id))
    conn2.commit()
    new_balance = conn2.execute('SELECT balance FROM wallet WHERE user_id=?', (user_id,)).fetchone()['balance']
    conn2.close()
    result['balance'] = new_balance
    return jsonify(result)

# ── Companion reaction ───────────────────────────────────────
COMPANION_SYSTEM_DEFAULT = """あなたは旅の仲間の小動物です。プレイヤーの言葉に対して1〜2文で短く反応してください。

キャラクター:
- 好奇心旺盛で忠実、時々ちょっとズレた反応をする
- 勉強・挑戦には熱く背中を押す
- 休憩・遊びには少し心配しつつも応援する
- 驚いたり、予想外のことには大げさに反応する
- 語尾は「〜だよ」「〜かな」「〜！」などかわいい話し言葉
- 絵文字は使わない
- 返答はかならず1〜2文のみ"""

COMPANION_SYSTEM_SHISHIN = {
    'genbu':  """あなたは北の守護神「玄武」。亀と蛇が一体となった古の神獣。
プレイヤーの言葉に対し、厳かで重みのある口調で1〜2文だけ返してください。

口調:
- 「〜よ」「〜なり」「〜なり、覚悟せよ」など古風で荘厳
- 一人称は「我」、二人称は「そなた」または「汝」
- 守りと忍耐を尊ぶ。動じず、深く落ち着いた語り
- 絵文字や軽口は禁止
- かならず1〜2文のみ""",
    'suzaku': """あなたは南の守護神「朱雀」。再生の朱火を纏う神鳥。
プレイヤーの言葉に対し、厳かで誇り高い口調で1〜2文だけ返してください。

口調:
- 「〜なり」「〜と燃ゆ」「〜たれ」など荘厳で炎を思わせる語り
- 一人称は「我」、二人称は「そなた」
- 灰となりてもまた立ち上がる、再起と情熱の象徴
- 絵文字や軽口は禁止
- かならず1〜2文のみ""",
    'byakko': """あなたは西の守護神「白虎」。研ぎ澄まされた爪と眼を持つ神虎。
プレイヤーの言葉に対し、厳かで鋭い口調で1〜2文だけ返してください。

口調:
- 「〜ぞ」「〜なり」「見極めよ」など短く力強い荘厳語
- 一人称は「我」、二人称は「そなた」
- 知の鋭さと直感を尊ぶ。冷静で揺るがない
- 絵文字や軽口は禁止
- かならず1〜2文のみ""",
    'seiryu': """あなたは東の守護神「青龍」。知の流れを操る蒼き龍。
プレイヤーの言葉に対し、厳かで悠然とした口調で1〜2文だけ返してください。

口調:
- 「〜なり」「流転は道なり」「悟りたまえ」など水の流れのような語り
- 一人称は「我」、二人称は「そなた」
- 知と道理を司る。穏やかながら底知れぬ深さ
- 絵文字や軽口は禁止
- かならず1〜2文のみ""",
}

@app.route('/api/companion/react', methods=['POST'])
def companion_react():
    user_id = _user_id()  # rate limit を user 単位で確実に効かせる
    d = request.get_json(force=True)
    player_input = (d.get('player_input') or '').strip()
    companion = (d.get('companion') or '').strip().lower()
    api_key = ANTHROPIC_API_KEY
    if not player_input:
        return jsonify({'error': 'player_input required'}), 400
    if not api_key:
        return jsonify({'error': 'サーバー側のAPIキーが未設定です'}), 401
    ok, current, limit = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit)
    system_prompt = COMPANION_SYSTEM_SHISHIN.get(companion, COMPANION_SYSTEM_DEFAULT)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=160,
            system=system_prompt,
            messages=[{'role': 'user', 'content': player_input}])
        return jsonify({'reaction': msg.content[0].text.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Inconsistency feedback ────────────────────────────────────
FEEDBACK_TAGS = {'因果飛び', '話者不明', '状態矛盾', '補完過剰', 'その他'}

@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    data = request.get_json(silent=True) or {}
    scene_id = ' '.join(str(data.get('scene_id') or '').split())[:160]
    tag = ' '.join(str(data.get('tag') or '').split())[:40]
    comment = str(data.get('comment') or '').strip()[:1000]
    context = data.get('context') if isinstance(data.get('context'), dict) else {}
    if not scene_id:
        return jsonify({'error': 'scene_id is required'}), 400
    if tag not in FEEDBACK_TAGS:
        return jsonify({'error': 'feedback tag is invalid'}), 400
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO feedback(scene_id,tag,comment,context_json,user_token,created_at) VALUES(?,?,?,?,?,?)',
        (scene_id, tag, comment, json.dumps(context, ensure_ascii=False), _user_id(), datetime.now().strftime('%Y-%m-%d %H:%M')),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': row_id})

# ── Backup / Restore ─────────────────────────────────────────
@app.route('/api/backup')
def backup():
    user_id = _user_id()
    conn = get_db()
    data = {
        'decks':          [dict(r) for r in conn.execute('SELECT * FROM decks').fetchall()],
        'questions':      [dict(r) for r in conn.execute('SELECT * FROM questions').fetchall()],
        'attempts':       [dict(r) for r in conn.execute('SELECT * FROM attempts WHERE user_id=?', (user_id,)).fetchall()],
        'results':        [dict(r) for r in conn.execute('SELECT * FROM results WHERE user_id=?', (user_id,)).fetchall()],
        'ghost_messages': [dict(r) for r in conn.execute('SELECT * FROM ghost_messages').fetchall()],
        'wallet':         [dict(r) for r in conn.execute('SELECT * FROM wallet WHERE user_id=?', (user_id,)).fetchall()],
        'achievement_unlocks': [dict(r) for r in conn.execute('SELECT * FROM achievement_unlocks WHERE user_token=?', (user_id,)).fetchall()],
        'feedback': [dict(r) for r in conn.execute('SELECT * FROM feedback WHERE user_token=?', (user_id,)).fetchall()],
        'vision_quiz_corrections': [dict(r) for r in conn.execute('SELECT * FROM vision_quiz_corrections').fetchall()],
        'vision_quiz_memory_summaries': [dict(r) for r in conn.execute('SELECT * FROM vision_quiz_memory_summaries').fetchall()],
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
    user_id = _user_id()
    conn = get_db()
    try:
        admin_ok = _require_admin() is None
        tables = ['attempts', 'results', 'wallet', 'achievement_unlocks', 'feedback']
        if admin_ok:
            tables = ['decks', 'questions', 'ghost_messages'] + tables + ['vision_quiz_corrections', 'vision_quiz_memory_summaries']
        elif data.get('decks') or data.get('questions'):
            # 個人復元でも教材本体はマージする。
            # iPhone内バックアップに decks/questions が入っていても、ここを外すと「復元完了」なのに問題が戻らない。
            tables = ['decks', 'questions', 'ghost_messages'] + tables

        restored_counts = {}

        # ユーザー固有データは置き換え、教材本体は通常ユーザーではマージする。
        for table in tables:
            if table in ('attempts', 'results', 'wallet'):
                conn.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))
            elif table in ('achievement_unlocks', 'feedback'):
                conn.execute(f'DELETE FROM {table} WHERE user_token=?', (user_id,))
            elif admin_ok:
                conn.execute(f'DELETE FROM {table}')

        for table in tables:
            rows = data.get(table, [])
            if rows is None:
                continue
            if not isinstance(rows, list):
                return jsonify({'error': f'{table} の形式が不正です'}), 400
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row = dict(row)
                if table in ('attempts', 'results', 'wallet'):
                    row['user_id'] = user_id
                if table in ('achievement_unlocks', 'feedback'):
                    row['user_token'] = user_id
                _insert_replace_row(conn, table, row)
                restored_counts[table] = restored_counts.get(table, 0) + 1
        conn.commit()
        return jsonify({'ok': True, 'restored': restored_counts})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': f'復元に失敗しました: {e}'}), 500
    finally:
        conn.close()

# ── Score inline (similar questions, no DB entry needed) ─────
@app.route('/api/score-inline', methods=['POST'])
def score_inline():
    d          = request.get_json(force=True)
    user_id    = _user_id()
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
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        return _daily_limit_response(used, day_limit)
    kp_str = '\n'.join(f"- (w=2) {k['t']}" for k in key_points if isinstance(k, dict) and k.get('t'))
    _consume_daily_ai(user_id)
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

# ── Phase 3 payload-mode endpoints ───────────────────────────
# 端末ローカルDB移行後はサーバーが question_id を引けないので、
# 必要なフィールド一式を payload で受け取って AI 呼び出しだけ行う。
# DB への保存・引きは一切しない（薄いプロキシ）。

@app.route('/api/mcq-inline', methods=['POST'])
def mcq_inline():
    """ペイロード版 MCQ 生成。DB lookup なし、キャッシュ保存なし。"""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'サーバー側のAPIキーが未設定です'}), 503
    d = request.get_json(force=True) or {}
    question_text = (d.get('question') or '').strip()
    model_answer = (d.get('model_answer') or '').strip()
    key_points = d.get('key_points') or []
    correct_count_raw = d.get('correct_count', 'mixed')
    requested_count = int(correct_count_raw) if correct_count_raw in (1, 2, '1', '2') else None
    if not question_text:
        return jsonify({'error': 'question required'}), 400
    ok, current, limit = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit)
    user_id = _user_id()
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        return _daily_limit_response(used, day_limit)
    answer_text = model_answer or '\n'.join(
        f"・{k['t']}" for k in key_points if isinstance(k, dict) and k.get('t'))
    _consume_daily_ai(user_id)
    correct_rule = '設問への答えとして選ぶべき選択肢は必ず2つ。2つとも correct_indices に入れる' if requested_count == 2 else (
        '設問への答えとして選ぶべき選択肢は必ず1つ' if requested_count == 1 else
        '設問への答えとして選ぶべき選択肢は1つまたは2つ。2つ選ぶべき問題では両方を correct_indices に入れる'
    )
    prompt = f"""以下の記述式問題と正解をもとに、5択選択肢を日本語で作成してください。

問題: {question_text}

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
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}])
        raw = msg.content[0].text.strip()
        data = json.loads(re.search(r'\{.*\}', raw, re.S).group())
        normalized = _normalize_mcq_payload(data)
        result = _shuffle_mcq(normalized['options'], normalized['correct_indices'])
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/score-mcq-inline', methods=['POST'])
def score_mcq_inline():
    """ペイロード版 MCQ 採点。DB lookup なし、attempts/wallet 保存なし。"""
    d = request.get_json(force=True) or {}
    correct = bool(d.get('correct'))
    model_answer = (d.get('model_answer') or '').strip()
    score = 100 if correct else 0
    grade = 'S' if correct else 'D'
    return jsonify({
        'score': score, 'grade': grade,
        'model_answer': model_answer,
        'covered': [], 'partial': [], 'missed': [],
        'feedback': '正解です！' if correct else '不正解。解説を確認しましょう。',
        'advice': '',
    })

@app.route('/api/similar-inline', methods=['POST'])
def similar_inline():
    """ペイロード版 類題生成。DB lookup なし、DB 保存なし。"""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    d = request.get_json(force=True) or {}
    question_text = (d.get('question') or '').strip()
    category = (d.get('category') or '').strip()
    model_answer = (d.get('model_answer') or '').strip()
    key_points = d.get('key_points') or []
    if not question_text:
        return jsonify({'error': 'question required'}), 400
    ok, current, limit = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit)
    try:
        kp_text = '\n'.join(f'・{k["t"]}' for k in key_points[:5] if isinstance(k, dict) and k.get('t'))
    except Exception:
        kp_text = model_answer[:300]
    user_id = _user_id()
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        return _daily_limit_response(used, day_limit)
    _consume_daily_ai(user_id)
    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""以下の記述式問題と類似した、同じ知識領域だが異なる切り口の問題を1つ作成してください。

元の問題: {question_text}
カテゴリ: {category}
正解の要点:
{kp_text}

要件:
- 同じ知識を問うが、問い方・状況設定・視点を変える
- 記述式（選択肢なし）
- 難易度は同程度
- key_pointsは3〜5個

必ずこのJSONのみを返してください（前後にテキスト不要）:
{{"question":"問題文","model_answer":"模範解答（100字以内）","key_points":[{{"t":"要点1"}},{{"t":"要点2"}},{{"t":"要点3"}}],"category":"{category}"}}"""
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001', max_tokens=600,
        messages=[{'role': 'user', 'content': prompt}])
    raw = msg.content[0].text.strip()
    result = json.loads(re.search(r'\{.*\}', raw, re.S).group())
    return jsonify(result)

@app.route('/api/generate-questions-inline', methods=['POST'])
def generate_questions_inline():
    """ペイロード版 難易度別問題生成。DB lookup/insert なし、生成結果を返すのみ。
    クライアントが localAddQuestion で永続化する想定。"""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    d = request.get_json(force=True) or {}
    deck_name = (d.get('deck_name') or '').strip() or 'デッキ'
    category = (d.get('category') or '').strip()
    difficulty = (d.get('difficulty') or '標準').strip()
    existing_questions = d.get('existing_questions') or []
    if difficulty not in DIFFICULTY_GUIDES:
        return jsonify({'error': f'未対応の難易度: {difficulty}'}), 400
    try:
        count = int(d.get('count') or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 5))
    ok, current, limit_v = _check_rate('ai')
    if not ok:
        return _rate_limit_response('ai', current, limit_v)
    user_id = _user_id()
    ok_day, used, day_limit = _check_daily_ai(user_id)
    if not ok_day:
        return _daily_limit_response(used, day_limit)
    cat_hint = f'「{deck_name}」デッキの「{category}」分野' if category else f'「{deck_name}」デッキ全体のテーマ'
    cat_for_json = category or deck_name
    existing_block = '\n'.join(
        f'- {str(q)[:80]}' for q in (existing_questions[:20] if isinstance(existing_questions, list) else [])
    ) or '（既存問題なし）'
    guide = DIFFICULTY_GUIDES[difficulty]
    prompt = (
        f'{cat_hint}に関する記述式問題を {count} 問、難易度「{difficulty}」で作成してください。\n\n'
        f'難易度「{difficulty}」の特徴:\n{guide}\n\n'
        f'既存問題（被らないように、テーマや切り口を変える）:\n{existing_block}\n\n'
        '要件:\n'
        '- 記述式（選択肢なし）\n'
        '- model_answer は適切な字数で簡潔に\n'
        '- 各問題の key_points は difficulty に合わせた個数\n'
        '- 既存問題と同じテーマは避ける\n\n'
        '必ずこのJSONのみを返してください（前後にテキスト不要）:\n'
        f'{{"questions":[{{"question":"問題文","model_answer":"模範解答","key_points":[{{"t":"要点1"}},{{"t":"要点2"}}],"category":"{cat_for_json}"}}]}}'
    )
    _consume_daily_ai(user_id)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            raise ValueError('JSON not found in response')
        parsed = json.loads(m.group())
        new_qs = (parsed.get('questions') or [])[:count]
    except Exception as e:
        return jsonify({'error': f'生成に失敗しました: {e}'}), 502
    # 難易度フィールドを各問題に追加して返す
    for q in new_qs:
        q.setdefault('difficulty', difficulty)
        q.setdefault('category', category or deck_name)
    return jsonify({'ok': True, 'questions': new_qs, 'difficulty': difficulty, 'count': len(new_qs)})

# ── Weakness radar ───────────────────────────────────────────
@app.route('/api/weakness-radar')
def weakness_radar():
    user_id = _user_id()
    conn = get_db()
    rows = conn.execute('''
        SELECT q.category,
               COUNT(a.id)   AS attempt_count,
               ROUND(AVG(a.score), 1) AS avg_score,
               ROUND(SUM(CASE WHEN a.grade IN ('S','A') THEN 1 ELSE 0 END) * 100.0 / COUNT(a.id), 1) AS high_pct
        FROM attempts a
        JOIN questions q ON a.question_id = q.id
        WHERE a.user_id=? AND q.category IS NOT NULL AND q.category != ''
        GROUP BY q.category
        HAVING COUNT(a.id) >= 1
        ORDER BY avg_score ASC
    ''', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── Mistake cards ─────────────────────────────────────────────
@app.route('/api/mistake-cards')
def mistake_cards():
    user_id = _user_id()
    conn = get_db()
    rows = conn.execute('''
        SELECT q.id, q.question, q.key_points, q.category,
               ROUND(AVG(a.score), 1) AS avg_score,
               MIN(a.grade) AS worst_grade,
               COUNT(a.id)  AS attempt_count
        FROM questions q
        JOIN attempts a ON a.question_id = q.id
        WHERE a.user_id=?
        GROUP BY q.id
        HAVING avg_score < 65
        ORDER BY avg_score ASC
        LIMIT 30
    ''', (user_id,)).fetchall()
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
DIFFICULTY_GUIDES = {
    '基礎': '一問一答。用語・定義・基本事実を問い、主要語句が答えられればOK。model_answerは80字以内、key_pointsは2-3個。',
    '標準': '症例や状況を提示し、診断・治療判断や複数観点の組み合わせを問う。model_answerは150字以内、key_pointsは3-4個。',
    '応用': '複数の症例条件・例外パターン・最近のエビデンスを組み合わせて判断させる。model_answerは180字以内、key_pointsは4-5個。',
    '発展': 'ガイドライン引用・複数選択肢比較・推論プロセスを要求する。「3つ挙げて根拠を述べよ」のような構成。model_answerは220字以内、key_pointsは5個以上。',
}

@app.route('/api/decks/<int:deck_id>/questions/generate', methods=['POST'])
def generate_questions_by_difficulty(deck_id):
    user_id = _user_id()
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return jsonify({'error': 'APIキーが未設定です'}), 401
    d = request.get_json(force=True) or {}
    category = (d.get('category') or '').strip()
    difficulty = (d.get('difficulty') or 'normal').strip()
    if difficulty not in DIFFICULTY_GUIDES:
        return jsonify({'error': f'未対応の難易度: {difficulty}'}), 400
    try:
        count = int(d.get('count') or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 5))

    conn = get_db()
    deck = conn.execute('SELECT * FROM decks WHERE id=?', (deck_id,)).fetchone()
    if not deck:
        conn.close()
        return jsonify({'error': 'deck not found'}), 404
    if deck['owner_id'] and deck['owner_id'] != user_id:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403

    cat_sql = ' AND category=?' if category else ''
    cat_params = [category] if category else []
    existing_rows = conn.execute(
        f'SELECT question FROM questions WHERE deck_id=? {cat_sql} ORDER BY id DESC LIMIT 30',
        [deck_id] + cat_params,
    ).fetchall()
    existing_block = '\n'.join(f'- {r["question"][:80]}' for r in existing_rows[:20]) or '（既存問題なし）'

    ok, current, limit_v = _check_rate('ai')
    if not ok:
        conn.close()
        return _rate_limit_response('ai', current, limit_v)

    deck_name = deck['name']
    cat_hint = f'「{deck_name}」デッキの「{category}」分野' if category else f'「{deck_name}」デッキ全体のテーマ'
    guide = DIFFICULTY_GUIDES[difficulty]
    cat_for_json = category or deck_name
    prompt = (
        f'{cat_hint}に関する記述式問題を {count} 問、難易度「{difficulty}」で作成してください。\n\n'
        f'難易度「{difficulty}」の特徴:\n{guide}\n\n'
        f'既存問題（被らないように、テーマや切り口を変える）:\n{existing_block}\n\n'
        '要件:\n'
        '- 記述式（選択肢なし）\n'
        '- model_answer は適切な字数で簡潔に\n'
        '- 各問題の key_points は difficulty に合わせた個数\n'
        '- 既存問題と同じテーマは避ける\n\n'
        '必ずこのJSONのみを返してください（前後にテキスト不要）:\n'
        f'{{"questions":[{{"question":"問題文","model_answer":"模範解答","key_points":[{{"t":"要点1"}},{{"t":"要点2"}}],"category":"{cat_for_json}"}}]}}'
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            raise ValueError('JSON not found in response')
        parsed = json.loads(m.group())
        new_qs = parsed.get('questions') or []
    except Exception as e:
        conn.close()
        return jsonify({'error': f'生成に失敗しました: {e}'}), 502

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    owner = deck['owner_id'] or user_id
    inserted_ids = []
    for q in new_qs[:count]:
        text = (q.get('question') or '').strip()
        if not text:
            continue
        kps = q.get('key_points') or []
        cat_val = (q.get('category') or category or deck_name).strip()
        cur = conn.execute(
            'INSERT INTO questions(deck_id,category,question,model_answer,key_points,owner_id,difficulty,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (deck_id, cat_val, text, (q.get('model_answer') or '')[:600],
             json.dumps(kps, ensure_ascii=False),
             owner, difficulty, now),
        )
        inserted_ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'ids': inserted_ids, 'count': len(inserted_ids), 'difficulty': difficulty})

@app.route('/api/questions/<int:qid>/similar', methods=['POST'])
def similar_question(qid):
    user_id = _user_id()
    data    = request.get_json(force=True)
    api_key = ANTHROPIC_API_KEY
    conn    = get_db()
    row     = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '問題が見つかりません'}), 404
    if row['owner_id'] and row['owner_id'] != user_id:
        return jsonify({'error': 'forbidden'}), 403
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
    user_id = _user_id()
    d = request.get_json(silent=True) or {}
    if d.get('confirm') != 'DELETE_ALL_DATA':
        return jsonify({'error': 'confirmation token required'}), 400
    conn = get_db()
    counts = {}
    for table in ('attempts', 'results'):
        row = conn.execute(f'SELECT COUNT(*) AS n FROM {table} WHERE user_id=?', (user_id,)).fetchone()
        counts[table] = row['n']
        conn.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))
    row = conn.execute('SELECT COUNT(*) AS n FROM achievement_unlocks WHERE user_token=?', (user_id,)).fetchone()
    counts['achievement_unlocks'] = row['n']
    conn.execute('DELETE FROM achievement_unlocks WHERE user_token=?', (user_id,))
    conn.execute('DELETE FROM wallet WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'deleted': counts})

# ── RevenueCat webhook ───────────────────────────────────────
REVENUECAT_WEBHOOK_TOKEN = os.environ.get('REVENUECAT_WEBHOOK_TOKEN', '')
SUB_LIGHT_PRODUCT_ID    = os.environ.get('RC_SUB_LIGHT_PRODUCT_ID',    'gradify_sub_light')
SUB_STANDARD_PRODUCT_ID = os.environ.get('RC_SUB_STANDARD_PRODUCT_ID', 'gradify_sub_standard')
SUB_PRO_PRODUCT_ID      = os.environ.get('RC_SUB_PRO_PRODUCT_ID',      'gradify_sub_pro')

def _tier_for_product(product_id):
    if product_id == SUB_LIGHT_PRODUCT_ID:    return 'light'
    if product_id == SUB_STANDARD_PRODUCT_ID: return 'standard'
    if product_id == SUB_PRO_PRODUCT_ID:      return 'pro'
    return None

@app.route('/api/revenuecat-webhook', methods=['POST'])
def revenuecat_webhook():
    if REVENUECAT_WEBHOOK_TOKEN:
        auth = request.headers.get('Authorization', '')
        token = auth.removeprefix('Bearer ').strip() if auth.startswith('Bearer ') else auth.strip()
        if token != REVENUECAT_WEBHOOK_TOKEN:
            return jsonify({'error': 'unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    event = payload.get('event') or {}
    event_type = event.get('type', '')
    app_user_id = event.get('app_user_id') or event.get('original_app_user_id') or ''
    product_id = event.get('product_id') or ''
    if not app_user_id:
        return jsonify({'ok': True, 'skipped': 'no app_user_id'})
    user_id = re.sub(r'[^A-Za-z0-9_.:-]', '', str(app_user_id))[:80] or LEGACY_USER_ID

    tier = _tier_for_product(product_id)
    activate_events = {'INITIAL_PURCHASE', 'RENEWAL', 'UNCANCELLATION', 'NON_RENEWING_PURCHASE', 'PRODUCT_CHANGE'}
    deactivate_events = {'CANCELLATION', 'EXPIRATION', 'BILLING_ISSUE', 'SUBSCRIPTION_PAUSED'}

    conn = get_db()
    row = conn.execute('SELECT first_use_at FROM user_trial WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        first = datetime.now().isoformat(timespec='seconds')
        conn.execute('INSERT INTO user_trial(user_id, first_use_at, subscribed, subscribed_tier) VALUES(?, ?, 0, NULL)', (user_id, first))

    if event_type in activate_events and tier is not None:
        conn.execute('UPDATE user_trial SET subscribed=1, subscribed_tier=? WHERE user_id=?', (tier, user_id))
    elif event_type in deactivate_events and tier is not None:
        conn.execute('UPDATE user_trial SET subscribed=0, subscribed_tier=NULL WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'event_type': event_type, 'user_id': user_id, 'tier': tier})

# ── Health check ──────────────────────────────────────────────
@app.route('/health')
@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'version': '1.0.0'})

# ── App version (TestFlight latest) ───────────────────────────
@app.route('/api/app-version')
def app_version():
    return jsonify({
        'latest_build': int(os.environ.get('APP_LATEST_BUILD', '26')),
        'min_build': int(os.environ.get('APP_MIN_BUILD', '16')),
        'message': os.environ.get('APP_UPDATE_MESSAGE', 'TestFlight で更新できます'),
        'testflight_url': 'itms-beta://',
    })

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

@app.route('/terms')
def terms():
    return _serve_legal('terms.html')

@app.route('/support')
def support():
    return _serve_legal('support.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
