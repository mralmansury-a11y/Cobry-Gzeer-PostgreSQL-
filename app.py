"""
منصة حجز الملاعب الرياضية - Stadium Booking Platform
Flask + PostgreSQL Backend
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
import psycopg2.extras
import hashlib
import os
from datetime import datetime, date, timedelta

app = Flask(__name__)
app.secret_key = "stadium_secret_key_2024"

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_X6rJtV3KTbwF@ep-odd-feather-ahe5hcyt-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)


# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        phone       TEXT UNIQUE NOT NULL,
        password    TEXT NOT NULL,
        role        TEXT DEFAULT 'player',
        created_at  TIMESTAMP DEFAULT NOW()
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS stadiums (
        id              SERIAL PRIMARY KEY,
        name            TEXT NOT NULL,
        city            TEXT NOT NULL,
        region          TEXT NOT NULL,
        location        TEXT NOT NULL,
        status          TEXT DEFAULT 'available',
        owner_id        INTEGER REFERENCES users(id),
        image_url       TEXT DEFAULT '',
        sport_type      TEXT DEFAULT 'football',
        price_per_hour  REAL DEFAULT 100.0,
        approved        INTEGER DEFAULT 1,
        created_at      TIMESTAMP DEFAULT NOW()
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id           SERIAL PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        stadium_id   INTEGER NOT NULL REFERENCES stadiums(id),
        booking_date TEXT NOT NULL,
        start_hour   INTEGER NOT NULL,
        booking_type TEXT DEFAULT 'single',
        sub_day      TEXT,
        sub_start    TEXT,
        sub_end      TEXT,
        notes        TEXT DEFAULT '',
        status       TEXT DEFAULT 'active',
        created_at   TIMESTAMP DEFAULT NOW()
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS add_requests (
        id             SERIAL PRIMARY KEY,
        owner_id       INTEGER NOT NULL REFERENCES users(id),
        name           TEXT NOT NULL,
        city           TEXT NOT NULL,
        region         TEXT NOT NULL,
        location       TEXT NOT NULL,
        status         TEXT DEFAULT 'available',
        sport_type     TEXT DEFAULT 'football',
        price_per_hour REAL DEFAULT 100.0,
        req_status     TEXT DEFAULT 'pending',
        created_at     TIMESTAMP DEFAULT NOW()
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id         SERIAL PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        message    TEXT NOT NULL,
        is_read    INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)

    # Seed users
    pw_admin  = hashlib.sha256("admin123".encode()).hexdigest()
    pw_owner  = hashlib.sha256("owner123".encode()).hexdigest()
    pw_player = hashlib.sha256("player123".encode()).hexdigest()

    c.execute("""
        INSERT INTO users (name,phone,password,role) VALUES (%s,%s,%s,%s)
        ON CONFLICT (phone) DO NOTHING
    """, ("مدير المنصة", "035", pw_admin, "admin"))

    c.execute("""
        INSERT INTO users (name,phone,password,role) VALUES (%s,%s,%s,%s)
        ON CONFLICT (phone) DO NOTHING
    """, ("أحمد المالك", "034", pw_owner, "owner"))

    c.execute("""
        INSERT INTO users (name,phone,password,role) VALUES (%s,%s,%s,%s)
        ON CONFLICT (phone) DO NOTHING
    """, ("محمد اللاعب", "033", pw_player, "player"))

    conn.commit()
    conn.close()


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


# ─────────────────────────────────────────────
#  CONFLICT-DETECTION ENGINE
# ─────────────────────────────────────────────

AR_DAY_TO_PY_WEEKDAY = {
    "الاثنين":  0,
    "الثلاثاء": 1,
    "الأربعاء": 2,
    "الخميس":  3,
    "الجمعة":  4,
    "السبت":   5,
    "الأحد":   6,
}


def _parse_date(d):
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _date_falls_on_weekday(d: date, target_weekday: int) -> bool:
    return d.weekday() == target_weekday


def _ranges_overlap(start1, end1, start2, end2) -> bool:
    if None in (start1, end1, start2, end2):
        return False
    return start1 <= end2 and start2 <= end1


def _subscription_covers_date(sub_day_name, sub_start, sub_end, target_date: date) -> bool:
    weekday = AR_DAY_TO_PY_WEEKDAY.get(sub_day_name)
    if weekday is None:
        return False
    start_d = _parse_date(sub_start)
    end_d   = _parse_date(sub_end)
    if not start_d or not end_d:
        return False
    if not (start_d <= target_date <= end_d):
        return False
    return _date_falls_on_weekday(target_date, weekday)


def check_single_booking_conflict(conn, stadium_id, booking_date_str, hour, exclude_booking_id=None):
    c = conn.cursor()
    target_date = _parse_date(booking_date_str)

    # 1) تعارض مع حجز مفرد آخر
    q = """
        SELECT id FROM bookings
        WHERE stadium_id=%s AND booking_date=%s AND start_hour=%s
          AND booking_type='single' AND status='active'
    """
    params = [stadium_id, booking_date_str, hour]
    if exclude_booking_id:
        q += " AND id != %s"
        params.append(exclude_booking_id)
    c.execute(q, params)
    if c.fetchone():
        return True, "هذه الساعة محجوزة بالفعل بحجز مفرد آخر"

    # 2) تعارض مع اشتراك نشط يغطي هذا التاريخ بنفس الساعة
    if target_date:
        c.execute("""
            SELECT id, sub_day, sub_start, sub_end FROM bookings
            WHERE stadium_id=%s AND start_hour=%s
              AND booking_type='subscription' AND status='active'
        """, (stadium_id, hour))
        subs = c.fetchall()
        for s in subs:
            if exclude_booking_id and s["id"] == exclude_booking_id:
                continue
            if _subscription_covers_date(s["sub_day"], s["sub_start"], s["sub_end"], target_date):
                return True, "هذه الساعة محجوزة ضمن اشتراك أسبوعي نشط لا يمكن حجزها بشكل مفرد"

    return False, None


def check_subscription_conflict(conn, stadium_id, sub_day, sub_start, sub_end, hour, exclude_booking_id=None):
    c = conn.cursor()
    weekday = AR_DAY_TO_PY_WEEKDAY.get(sub_day)
    new_start = _parse_date(sub_start)
    new_end   = _parse_date(sub_end)

    # 1) تعارض مع اشتراك آخر نشط بنفس اليوم/الساعة وفترة متقاطعة
    q = """
        SELECT id, sub_start, sub_end FROM bookings
        WHERE stadium_id=%s AND sub_day=%s AND start_hour=%s
          AND booking_type='subscription' AND status='active'
    """
    params = [stadium_id, sub_day, hour]
    if exclude_booking_id:
        q += " AND id != %s"
        params.append(exclude_booking_id)
    c.execute(q, params)
    other_subs = c.fetchall()
    for s in other_subs:
        other_start = _parse_date(s["sub_start"])
        other_end   = _parse_date(s["sub_end"])
        if _ranges_overlap(new_start, new_end, other_start, other_end):
            return True, (
                f"يوجد اشتراك آخر نشط على نفس اليوم ({sub_day}) ونفس الساعة "
                f"يتقاطع مع الفترة المطلوبة ({s['sub_start']} → {s['sub_end']})"
            )

    # 2) تعارض مع حجوزات مفردة نشطة تقع على إحدى جلسات هذا الاشتراك
    if weekday is not None and new_start and new_end:
        c.execute("""
            SELECT id, booking_date FROM bookings
            WHERE stadium_id=%s AND start_hour=%s
              AND booking_type='single' AND status='active'
              AND booking_date >= %s AND booking_date <= %s
        """, (stadium_id, hour, sub_start, sub_end))
        singles = c.fetchall()
        for b in singles:
            bd = _parse_date(b["booking_date"])
            if bd and _date_falls_on_weekday(bd, weekday):
                return True, (
                    f"يوجد حجز مفرد بتاريخ {b['booking_date']} على نفس الساعة "
                    f"يتعارض مع جلسات هذا الاشتراك"
                )

    return False, None


# ─────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login_page"))

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/register")
def register_page():
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    phone    = data.get("phone", "").strip()
    password = hash_pw(data.get("password", ""))
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE phone=%s AND password=%s", (phone, password))
    user = c.fetchone()
    conn.close()
    if not user:
        return jsonify({"success": False, "message": "رقم الهاتف أو كلمة المرور غير صحيحة"})
    session["user_id"]   = user["id"]
    session["user_name"] = user["name"]
    session["user_role"] = user["role"]
    return jsonify({"success": True, "role": user["role"], "name": user["name"]})

@app.route("/api/register", methods=["POST"])
def api_register():
    data    = request.json or {}
    name    = data.get("name", "").strip()
    phone   = data.get("phone", "").strip()
    password= data.get("password", "")
    confirm = data.get("confirm", "")
    role    = data.get("role", "player")
    if not all([name, phone, password]):
        return jsonify({"success": False, "message": "جميع الحقول مطلوبة"})
    if password != confirm:
        return jsonify({"success": False, "message": "كلمتا المرور غير متطابقتين"})
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name,phone,password,role) VALUES (%s,%s,%s,%s)",
                  (name, phone, hash_pw(password), role))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "تم إنشاء الحساب بنجاح"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": "رقم الهاتف مسجل مسبقاً"})


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    role = session.get("user_role")
    if role == "admin":
        return render_template("admin.html")
    elif role == "owner":
        return render_template("owner.html")
    return render_template("player.html")


# ─────────────────────────────────────────────
#  SHARED SESSION / STATS
# ─────────────────────────────────────────────
@app.route("/api/session")
def get_session():
    return jsonify({
        "user_id":   session.get("user_id"),
        "user_name": session.get("user_name"),
        "user_role": session.get("user_role"),
    })

@app.route("/api/profile")
def get_profile():
    if "user_id" not in session:
        return jsonify({})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id,name,phone,role FROM users WHERE id=%s", (session["user_id"],))
    user = c.fetchone()
    conn.close()
    return jsonify(dict(user))

@app.route("/api/profile/update", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"})
    data  = request.json or {}
    name  = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    pw    = data.get("password", "")
    if not name or not phone:
        return jsonify({"success": False, "message": "الاسم ورقم الهاتف مطلوبان"})
    conn = get_db()
    c = conn.cursor()
    try:
        if pw:
            c.execute("UPDATE users SET name=%s,phone=%s,password=%s WHERE id=%s",
                      (name, phone, hash_pw(pw), session["user_id"]))
        else:
            c.execute("UPDATE users SET name=%s,phone=%s WHERE id=%s",
                      (name, phone, session["user_id"]))
        conn.commit()
        session["user_name"] = name
        conn.close()
        return jsonify({"success": True, "message": "تم تحديث البيانات بنجاح ✅"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": "رقم الهاتف مسجل مسبقاً"})

@app.route("/api/notifications")
def get_notifications():
    if "user_id" not in session:
        return jsonify([])
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 20",
        (session["user_id"],)
    )
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/notifications/read", methods=["POST"])
def mark_notifications_read():
    if "user_id" not in session:
        return jsonify({"success": False}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE notifications SET is_read=1 WHERE user_id=%s", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ─────────────────────────────────────────────
#  PLAYER APIs
# ─────────────────────────────────────────────
@app.route("/api/stadiums")
def get_stadiums():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT s.*, u.name as owner_name
        FROM stadiums s LEFT JOIN users u ON s.owner_id=u.id
        WHERE s.approved=1
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/bookings/my")
def my_bookings():
    if "user_id" not in session:
        return jsonify([])
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT b.*, s.name as stadium_name, s.city, s.sport_type
        FROM bookings b JOIN stadiums s ON b.stadium_id=s.id
        WHERE b.user_id=%s AND b.status='active'
        ORDER BY b.booking_date DESC
    """, (session["user_id"],))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/book", methods=["POST"])
def book_stadium():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"})
    data = request.json or {}
    sid  = data.get("stadium_id")
    date_str = data.get("booking_date")
    hour = data.get("start_hour")
    if not all([sid, date_str]) or hour is None:
        return jsonify({"success": False, "message": "بيانات الحجز غير مكتملة"})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM stadiums WHERE id=%s AND status='available'", (sid,))
    stadium = c.fetchone()
    if not stadium:
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير متاح للحجز"})

    conflict, reason = check_single_booking_conflict(conn, sid, date_str, hour)
    if conflict:
        conn.close()
        return jsonify({"success": False, "message": reason or "الساعة محجوزة"})

    c.execute("""
        INSERT INTO bookings (user_id,stadium_id,booking_date,start_hour,booking_type)
        VALUES (%s,%s,%s,%s,'single')
    """, (session["user_id"], sid, date_str, hour))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم الحجز بنجاح ✅"})

@app.route("/api/stadium/<int:sid>/booked-hours")
def stadium_booked_hours(sid):
    date_str = request.args.get("date", "").strip()
    target_date = _parse_date(date_str)
    if not target_date:
        return jsonify([])

    conn = get_db()
    c = conn.cursor()
    booked = set()

    c.execute("""
        SELECT start_hour FROM bookings
        WHERE stadium_id=%s AND booking_date=%s AND booking_type='single' AND status='active'
    """, (sid, date_str))
    for r in c.fetchall():
        booked.add(r["start_hour"])

    c.execute("""
        SELECT start_hour, sub_day, sub_start, sub_end FROM bookings
        WHERE stadium_id=%s AND booking_type='subscription' AND status='active'
    """, (sid,))
    for s in c.fetchall():
        if _subscription_covers_date(s["sub_day"], s["sub_start"], s["sub_end"], target_date):
            booked.add(s["start_hour"])

    conn.close()
    return jsonify(sorted(booked))


@app.route("/api/subscribe", methods=["POST"])
def subscribe_stadium():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول"})
    data  = request.json or {}
    sid   = data.get("stadium_id")
    day   = data.get("day")
    hour  = data.get("hour")
    start = data.get("start_date")
    end   = data.get("end_date")

    if not all([sid, day, start, end]) or hour is None:
        return jsonify({"success": False, "message": "أكمل جميع الحقول المطلوبة"})
    if start >= end:
        return jsonify({"success": False, "message": "تاريخ الانتهاء يجب أن يكون بعد تاريخ البداية"})

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM stadiums WHERE id=%s AND status='available'", (sid,))
    stadium = c.fetchone()
    if not stadium:
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير متاح للاشتراك"})

    conflict, reason = check_subscription_conflict(conn, sid, day, start, end, hour)
    if conflict:
        conn.close()
        return jsonify({"success": False, "message": reason or "هذا الموعد الأسبوعي محجوز"})

    c.execute("""
        INSERT INTO bookings
            (user_id,stadium_id,booking_date,start_hour,booking_type,sub_day,sub_start,sub_end)
        VALUES (%s,%s,%s,%s,'subscription',%s,%s,%s)
    """, (session["user_id"], sid, start, hour, day, start, end))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم الاشتراك بنجاح ✅"})

@app.route("/api/bookings/delete/<int:bid>", methods=["DELETE"])
def delete_booking(bid):
    if "user_id" not in session:
        return jsonify({"success": False})
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE bookings SET status='cancelled' WHERE id=%s AND user_id=%s",
        (bid, session["user_id"])
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم حذف الحجز بنجاح"})


# ─────────────────────────────────────────────
#  OWNER APIs
# ─────────────────────────────────────────────

def _owner_required():
    if "user_id" not in session:
        return None
    if session.get("user_role") not in ("owner", "admin"):
        return None
    return session["user_id"]


@app.route("/api/owner/stadiums")
def owner_stadiums():
    uid = _owner_required()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM stadiums WHERE owner_id=%s ORDER BY created_at DESC", (uid,))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/owner/stadium/<int:sid>/bookings")
def stadium_bookings(sid):
    uid = _owner_required()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM stadiums WHERE id=%s AND owner_id=%s", (sid, uid))
    stadium = c.fetchone()
    if not stadium:
        conn.close()
        return jsonify({"error": "not found or unauthorized"}), 404
    c.execute("""
        SELECT b.*, u.name as user_name, u.phone as user_phone
        FROM bookings b JOIN users u ON b.user_id=u.id
        WHERE b.stadium_id=%s AND b.status='active'
        ORDER BY b.booking_date, b.start_hour
    """, (sid,))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/owner/bookings/add", methods=["POST"])
def owner_add_booking():
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401

    data         = request.json or {}
    stadium_id   = data.get("stadium_id")
    player_phone = str(data.get("player_phone", "")).strip()
    booking_date = data.get("booking_date", "")
    start_hour   = data.get("start_hour")

    if not all([stadium_id, player_phone, booking_date]) or start_hour is None:
        return jsonify({"success": False, "message": "أكمل جميع الحقول المطلوبة"})

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM stadiums WHERE id=%s AND owner_id=%s", (stadium_id, uid))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير موجود أو ليس لك صلاحية عليه"})

    c.execute("SELECT id FROM users WHERE phone=%s", (player_phone,))
    player = c.fetchone()
    if not player:
        conn.close()
        return jsonify({"success": False, "message": "لم يتم العثور على لاعب بهذا الرقم"})

    try:
        hour = int(start_hour)
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"success": False, "message": "الساعة غير صحيحة"})

    conflict, reason = check_single_booking_conflict(conn, stadium_id, booking_date, hour)
    if conflict:
        conn.close()
        return jsonify({"success": False, "message": reason or "هذه الساعة محجوزة بالفعل"})

    c.execute("""
        INSERT INTO bookings (user_id,stadium_id,booking_date,start_hour,booking_type,status)
        VALUES (%s,%s,%s,%s,'single','active')
    """, (player["id"], stadium_id, booking_date, hour))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم إضافة الحجز بنجاح ✅"})


@app.route("/api/owner/stadium/update/<int:sid>", methods=["POST"])
def update_stadium(sid):
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401
    data = request.json or {}
    for field in ["name", "city", "region", "location", "status"]:
        if not str(data.get(field, "")).strip():
            return jsonify({"success": False, "message": f"الحقل {field} مطلوب"})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM stadiums WHERE id=%s AND owner_id=%s", (sid, uid))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير موجود أو ليس لك صلاحية تعديله"})
    c.execute("""
        UPDATE stadiums
        SET name=%s, city=%s, region=%s, location=%s, status=%s, sport_type=%s, price_per_hour=%s
        WHERE id=%s AND owner_id=%s
    """, (
        data["name"].strip(), data["city"].strip(), data["region"].strip(),
        data["location"].strip(), data["status"],
        data.get("sport_type", "football"),
        float(data.get("price_per_hour", 100)),
        sid, uid
    ))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم تحديث بيانات الملعب ✅"})


@app.route("/api/owner/request", methods=["POST"])
def add_stadium_request():
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401
    data     = request.json or {}
    name     = data.get("name", "").strip()
    city     = data.get("city", "").strip()
    region   = data.get("region", "").strip()
    location = data.get("location", "").strip()
    if not all([name, city, location]):
        return jsonify({"success": False, "message": "اسم الملعب والمدينة والموقع مطلوبة"})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM add_requests WHERE location=%s AND req_status='pending'", (location,))
    if c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "يوجد طلب معلق بنفس الموقع"})
    c.execute("""
        INSERT INTO add_requests (owner_id,name,city,region,location,status,sport_type,price_per_hour)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (uid, name, city, region, location,
          data.get("status", "available"),
          data.get("sport_type", "football"),
          float(data.get("price_per_hour", 100))))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم إرسال طلب إضافة الملعب بنجاح ✅"})


@app.route("/api/owner/requests")
def owner_requests():
    uid = _owner_required()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM add_requests WHERE owner_id=%s ORDER BY created_at DESC", (uid,))
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/owner/stats")
def owner_stats():
    uid = _owner_required()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stadiums WHERE owner_id=%s", (uid,))
    stadiums_count = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) FROM stadiums WHERE owner_id=%s AND status='available'", (uid,))
    available_count = c.fetchone()["count"]
    c.execute("""
        SELECT COUNT(*) FROM bookings b
        JOIN stadiums s ON b.stadium_id=s.id
        WHERE s.owner_id=%s AND b.status='active' AND b.booking_type='single'
    """, (uid,))
    bookings_count = c.fetchone()["count"]
    c.execute("""
        SELECT COUNT(*) FROM bookings b
        JOIN stadiums s ON b.stadium_id=s.id
        WHERE s.owner_id=%s AND b.status='active' AND b.booking_type='subscription'
    """, (uid,))
    subs_count = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) FROM add_requests WHERE owner_id=%s AND req_status='pending'", (uid,))
    pending_requests = c.fetchone()["count"]
    conn.close()
    return jsonify({
        "stadiums":         stadiums_count,
        "available":        available_count,
        "bookings":         bookings_count,
        "subscriptions":    subs_count,
        "pending_requests": pending_requests,
    })


@app.route("/api/owner/lookup-player")
def lookup_player():
    uid = _owner_required()
    if not uid:
        return jsonify({"found": False}), 401
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"found": False})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name, phone FROM users WHERE phone=%s AND role='player'", (phone,))
    user = c.fetchone()
    conn.close()
    if user:
        return jsonify({"found": True, "id": user["id"], "name": user["name"]})
    return jsonify({"found": False})


@app.route("/api/owner/subscriptions")
def owner_subscriptions():
    uid = _owner_required()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401

    sid_filter = request.args.get("stadium_id", "").strip()
    conn = get_db()
    c = conn.cursor()

    base_query = """
        SELECT
            b.*,
            u.name        AS user_name,
            u.phone       AS user_phone,
            s.name        AS stadium_name,
            s.price_per_hour AS stadium_price,
            s.sport_type  AS sport_type
        FROM bookings b
        JOIN users    u ON b.user_id    = u.id
        JOIN stadiums s ON b.stadium_id = s.id
        WHERE s.owner_id = %s
          AND b.booking_type = 'subscription'
        ORDER BY b.created_at DESC
    """

    if sid_filter:
        c.execute(
            base_query.replace("ORDER BY", "AND b.stadium_id = %s ORDER BY"),
            (uid, int(sid_filter))
        )
    else:
        c.execute(base_query, (uid,))

    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/owner/subscriptions/add", methods=["POST"])
def owner_add_subscription():
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401

    data         = request.json or {}
    stadium_id   = data.get("stadium_id")
    player_phone = str(data.get("player_phone", "")).strip()
    day          = data.get("day", "")
    hour         = data.get("hour")
    start_date   = data.get("start_date", "")
    end_date     = data.get("end_date", "")
    notes        = data.get("notes", "")

    if not all([stadium_id, player_phone, day, hour, start_date, end_date]):
        return jsonify({"success": False, "message": "أكمل جميع الحقول المطلوبة"})
    if start_date >= end_date:
        return jsonify({"success": False, "message": "تاريخ الانتهاء يجب أن يكون بعد تاريخ البداية"})

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM stadiums WHERE id=%s AND owner_id=%s", (stadium_id, uid))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير موجود أو ليس لك صلاحية عليه"})

    c.execute("SELECT id FROM users WHERE phone=%s", (player_phone,))
    player = c.fetchone()
    if not player:
        conn.close()
        return jsonify({"success": False, "message": "لم يتم العثور على لاعب بهذا الرقم"})

    conflict, reason = check_subscription_conflict(conn, stadium_id, day, start_date, end_date, int(hour))
    if conflict:
        conn.close()
        return jsonify({"success": False, "message": reason or "هذا الموعد الأسبوعي يتعارض مع اشتراك آخر"})

    c.execute("""
        INSERT INTO bookings
            (user_id, stadium_id, booking_date, start_hour,
             booking_type, sub_day, sub_start, sub_end, notes, status)
        VALUES (%s,%s,%s,%s,'subscription',%s,%s,%s,%s,'active')
    """, (player["id"], stadium_id, start_date, int(hour), day, start_date, end_date, notes))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم إضافة الاشتراك بنجاح ✅"})


@app.route("/api/owner/subscriptions/update/<int:bid>", methods=["POST"])
def owner_update_subscription(bid):
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401

    data = request.json or {}
    start_date = data.get("start_date", "")
    end_date   = data.get("end_date", "")
    if start_date and end_date and start_date >= end_date:
        return jsonify({"success": False, "message": "تاريخ الانتهاء يجب أن يكون بعد تاريخ البداية"})

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT b.id, b.stadium_id FROM bookings b
        JOIN stadiums s ON b.stadium_id = s.id
        WHERE b.id=%s AND s.owner_id=%s AND b.booking_type='subscription'
    """, (bid, uid))
    booking = c.fetchone()
    if not booking:
        conn.close()
        return jsonify({"success": False, "message": "الاشتراك غير موجود أو ليس لك صلاحية تعديله"})

    new_status = data.get("status", "active")
    new_day    = data.get("day")
    new_hour   = int(data.get("hour", 8))

    if new_status == "active":
        conflict, reason = check_subscription_conflict(
            conn, booking["stadium_id"], new_day, start_date, end_date, new_hour,
            exclude_booking_id=bid
        )
        if conflict:
            conn.close()
            return jsonify({"success": False, "message": reason or "هذا الموعد الأسبوعي يتعارض مع اشتراك آخر"})

    c.execute("""
        UPDATE bookings
        SET sub_day=%s, start_hour=%s, sub_start=%s, sub_end=%s, status=%s, notes=%s
        WHERE id=%s
    """, (new_day, new_hour, start_date, end_date, new_status, data.get("notes", ""), bid))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم تحديث الاشتراك ✅"})


@app.route("/api/owner/subscriptions/cancel/<int:bid>", methods=["POST"])
def owner_cancel_subscription(bid):
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT b.id FROM bookings b
        JOIN stadiums s ON b.stadium_id = s.id
        WHERE b.id=%s AND s.owner_id=%s AND b.booking_type='subscription'
    """, (bid, uid))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الاشتراك غير موجود أو ليس لك صلاحية إلغائه"})
    c.execute("UPDATE bookings SET status='cancelled' WHERE id=%s", (bid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم إلغاء الاشتراك"})


@app.route("/api/owner/subscriptions/delete/<int:bid>", methods=["DELETE"])
def owner_delete_subscription(bid):
    uid = _owner_required()
    if not uid:
        return jsonify({"success": False, "message": "يجب تسجيل الدخول أولاً"}), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT b.id FROM bookings b
        JOIN stadiums s ON b.stadium_id = s.id
        WHERE b.id=%s AND s.owner_id=%s AND b.booking_type='subscription'
    """, (bid, uid))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الاشتراك غير موجود أو ليس لك صلاحية حذفه"})
    c.execute("DELETE FROM bookings WHERE id=%s", (bid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم حذف الاشتراك نهائياً"})


# ─────────────────────────────────────────────
#  ADMIN APIs
# ─────────────────────────────────────────────

def _admin_required():
    if "user_id" not in session or session.get("user_role") != "admin":
        return None
    return session["user_id"]


@app.route("/api/stats")
def get_stats():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) FROM stadiums WHERE approved=1")
    stadiums = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) FROM bookings WHERE status='active'")
    bookings = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) FROM add_requests WHERE req_status='pending'")
    requests = c.fetchone()["count"]
    conn.close()
    return jsonify({"users": users, "stadiums": stadiums, "bookings": bookings, "requests": requests})


@app.route("/api/admin/users")
def admin_users():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id,name,phone,role,created_at FROM users ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users/add", methods=["POST"])
def admin_add_user():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name,phone,password,role) VALUES (%s,%s,%s,%s)",
                  (data["name"], data["phone"], hash_pw(data["password"]), data.get("role","player")))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "تم إضافة المستخدم ✅"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": "رقم الهاتف مسجل مسبقاً"})


@app.route("/api/admin/users/delete/<int:uid>", methods=["DELETE"])
def admin_delete_user(uid):
    admin_uid = _admin_required()
    if not admin_uid:
        return jsonify({"error": "unauthorized"}), 403

    if uid == admin_uid:
        return jsonify({"success": False, "message": "لا يمكنك حذف حسابك الخاص"})

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, role, name FROM users WHERE id=%s", (uid,))
    target = c.fetchone()
    if not target:
        conn.close()
        return jsonify({"success": False, "message": "المستخدم غير موجود"})

    if target["role"] == "admin":
        c.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
        if c.fetchone()["count"] <= 1:
            conn.close()
            return jsonify({"success": False, "message": "لا يمكن حذف آخر حساب مدير في النظام"})

    try:
        c.execute("DELETE FROM bookings WHERE user_id=%s", (uid,))
        c.execute("SELECT id FROM stadiums WHERE owner_id=%s", (uid,))
        owned = c.fetchall()
        for st in owned:
            c.execute("DELETE FROM bookings WHERE stadium_id=%s", (st["id"],))
        c.execute("DELETE FROM stadiums WHERE owner_id=%s", (uid,))
        c.execute("DELETE FROM add_requests WHERE owner_id=%s", (uid,))
        c.execute("DELETE FROM notifications WHERE user_id=%s", (uid,))
        c.execute("DELETE FROM users WHERE id=%s", (uid,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": f"فشل حذف المستخدم: {e}"})

    conn.close()
    return jsonify({"success": True, "message": f"تم حذف المستخدم '{target['name']}' نهائياً ✅"})


@app.route("/api/admin/stadiums")
def admin_stadiums():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT s.*, u.name as owner_name
        FROM stadiums s LEFT JOIN users u ON s.owner_id=u.id
        ORDER BY s.created_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/stadiums/add", methods=["POST"])
def admin_add_stadium():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM stadiums WHERE location=%s", (data["location"],))
    if c.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "الملعب موجود بالفعل"})
    c.execute("""
        INSERT INTO stadiums (name,city,region,location,status,sport_type,price_per_hour,approved)
        VALUES (%s,%s,%s,%s,%s,%s,%s,1)
    """, (data["name"], data["city"], data["region"], data["location"],
          data.get("status","available"), data.get("sport_type","football"),
          data.get("price_per_hour",100)))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم إضافة الملعب ✅"})


@app.route("/api/admin/stadiums/delete/<int:sid>", methods=["DELETE"])
def admin_delete_stadium(sid):
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name FROM stadiums WHERE id=%s", (sid,))
    stadium = c.fetchone()
    if not stadium:
        conn.close()
        return jsonify({"success": False, "message": "الملعب غير موجود"})

    try:
        c.execute("DELETE FROM bookings WHERE stadium_id=%s", (sid,))
        c.execute("DELETE FROM stadiums WHERE id=%s", (sid,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": f"فشل حذف الملعب: {e}"})

    conn.close()
    return jsonify({"success": True, "message": f"تم حذف الملعب '{stadium['name']}' نهائياً ✅"})


@app.route("/api/admin/requests")
def admin_requests():
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT r.*, u.name as owner_name, u.phone as owner_phone
        FROM add_requests r JOIN users u ON r.owner_id=u.id
        WHERE r.req_status='pending'
        ORDER BY r.created_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/requests/accept/<int:rid>", methods=["POST"])
def accept_request(rid):
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM add_requests WHERE id=%s", (rid,))
    req = c.fetchone()
    if not req:
        conn.close()
        return jsonify({"success": False, "message": "الطلب غير موجود"})
    c.execute("""
        INSERT INTO stadiums (name,city,region,location,status,owner_id,sport_type,price_per_hour,approved)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)
    """, (req["name"], req["city"], req["region"], req["location"],
          req["status"], req["owner_id"], req["sport_type"], req["price_per_hour"]))
    c.execute("UPDATE add_requests SET req_status='accepted' WHERE id=%s", (rid,))
    c.execute(
        "INSERT INTO notifications (user_id, message) VALUES (%s,%s)",
        (req["owner_id"], f"تم قبول طلب إضافة ملعب '{req['name']}' ✅")
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم قبول الطلب وإضافة الملعب ✅"})


@app.route("/api/admin/requests/reject/<int:rid>", methods=["DELETE"])
def reject_request(rid):
    if not _admin_required():
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT owner_id, name FROM add_requests WHERE id=%s", (rid,))
    req = c.fetchone()
    if req:
        c.execute(
            "INSERT INTO notifications (user_id, message) VALUES (%s,%s)",
            (req["owner_id"], f"تم رفض طلب إضافة ملعب '{req['name']}'")
        )
    c.execute("UPDATE add_requests SET req_status='rejected' WHERE id=%s", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم رفض الطلب"})


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("✅  Stadium Booking Platform (PostgreSQL) — http://localhost:5050")
    print("📋  Accounts:")
    print("    Admin  → 035 / admin123")
    print("    Owner  → 034 / owner123")
    print("    Player → 033 / player123")
    app.run(debug=True, port=5050)
