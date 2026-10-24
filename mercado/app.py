#!/usr/bin/env python3
"""Mercado Pro - local-first supermarket management app. Python stdlib only."""
from __future__ import annotations
import csv, hashlib, hmac, io, json, os, secrets, shutil, sqlite3, sys, time, traceback
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MERCADO_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "mercado.sqlite3"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
HOST = os.environ.get("MERCADO_HOST", "127.0.0.1")
PORT = int(os.environ.get("MERCADO_PORT", "8765"))
SESSION_SECONDS = 12 * 60 * 60
MAX_BODY = 2 * 1024 * 1024

SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','manager','cashier')),
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf_token TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 color TEXT NOT NULL DEFAULT '#32d583', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, document TEXT NOT NULL DEFAULT '',
 contact TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
 email TEXT NOT NULL DEFAULT '', document TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
 barcode TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
 unit TEXT NOT NULL DEFAULT 'un', cost_cents INTEGER NOT NULL CHECK(cost_cents >= 0),
 price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
 stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(stock_milli >= 0),
 min_stock_milli INTEGER NOT NULL DEFAULT 0 CHECK(min_stock_milli >= 0),
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode) WHERE barcode <> '';
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
CREATE TABLE IF NOT EXISTS discounts (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_type TEXT NOT NULL CHECK(target_type IN ('product','category')),
 target_id INTEGER NOT NULL, percent_bp INTEGER NOT NULL CHECK(percent_bp > 0 AND percent_bp <= 9900),
 starts_at TEXT NOT NULL DEFAULT '', ends_at TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discounts_target ON discounts(target_type,target_id,active);
CREATE TABLE IF NOT EXISTS sales (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
 customer_name TEXT NOT NULL DEFAULT '', payment_method TEXT NOT NULL,
 subtotal_cents INTEGER NOT NULL, discount_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL,
 cost_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'completed' CHECK(status IN ('completed','cancelled')),
 notes TEXT NOT NULL DEFAULT '', created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, cancelled_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
CREATE TABLE IF NOT EXISTS sale_items (
 id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
 product_id INTEGER REFERENCES products(id) ON DELETE SET NULL, sku TEXT NOT NULL, product_name TEXT NOT NULL, category_name TEXT NOT NULL DEFAULT 'Sem categoria',
 quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0), unit_price_cents INTEGER NOT NULL,
 discount_bp INTEGER NOT NULL DEFAULT 0, line_subtotal_cents INTEGER NOT NULL,
 line_discount_cents INTEGER NOT NULL, line_total_cents INTEGER NOT NULL,
 unit_cost_cents INTEGER NOT NULL, line_cost_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_items_product ON sale_items(product_id);
CREATE TABLE IF NOT EXISTS stock_movements (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 kind TEXT NOT NULL CHECK(kind IN ('entry','sale','adjustment','return','cancellation','purchase')),
 quantity_milli INTEGER NOT NULL, before_milli INTEGER NOT NULL, after_milli INTEGER NOT NULL,
 unit_cost_cents INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', reference_type TEXT NOT NULL DEFAULT '',
 reference_id INTEGER, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id,created_at DESC);
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY, category TEXT NOT NULL, description TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), expense_date TEXT NOT NULL,
 payment_method TEXT NOT NULL DEFAULT 'outro', notes TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
CREATE TABLE IF NOT EXISTS purchase_orders (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','received','cancelled')),
 total_cents INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
 id INTEGER PRIMARY KEY, purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
 quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0), unit_cost_cents INTEGER NOT NULL CHECK(unit_cost_cents >= 0),
 line_total_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL, entity TEXT NOT NULL, entity_id INTEGER, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""


def now_iso(): return datetime.now().astimezone().isoformat(timespec="seconds")
def today_iso(): return date.today().isoformat()
def money_to_cents(value):
    try:
        v = Decimal(str(value).strip().replace("R$", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError): raise ValueError("Valor monetário inválido.")
    if not v.is_finite() or v < 0 or v > Decimal("999999999.99"): raise ValueError("Valor monetário fora do limite permitido.")
    return int((v * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
def cents_to_money(c): return int(c) / 100
def qty_to_milli(value, allow_zero=False):
    try: q = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError): raise ValueError("Quantidade inválida.")
    if not q.is_finite() or q < (Decimal(0) if allow_zero else Decimal("0.001")) or q > Decimal("999999999"):
        raise ValueError("Quantidade fora do limite permitido.")
    return int((q * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
def milli_to_qty(q): return int(q) / 1000
def mul_money(cents, milli): return (int(cents) * int(milli) + 500) // 1000
def pct_to_bp(value):
    try: p = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError): raise ValueError("Percentual inválido.")
    if not p.is_finite() or p <= 0 or p > 99: raise ValueError("O desconto deve ser maior que zero e de até 99%.")
    bp = int((p * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if bp < 1: raise ValueError("O menor desconto permitido é 0,01%.")
    return bp
def bp_to_pct(bp): return int(bp) / 100
COUNT_UNITS = {"un", "pct", "cx", "fardo"}
def validate_count_unit(unit, quantity, label="quantidade"):
    if unit in COUNT_UNITS and int(quantity) % 1000 != 0:
        raise ValueError(f"Para produtos vendidos por {unit}, a {label} precisa ser um número inteiro.")
def hash_password(password, salt=None):
    if not isinstance(password, str) or len(password) < 10: raise ValueError("A senha deve ter ao menos 10 caracteres.")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return salt.hex() + "$" + derived.hex()
def verify_password(password, stored):
    try:
        salt_hex, expected = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310_000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception: return False

@contextmanager
def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=FULL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    with connect_db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # Lightweight forward migrations for databases created by older builds.
        sale_item_columns = {r["name"] for r in con.execute("PRAGMA table_info(sale_items)")}
        if "category_name" not in sale_item_columns:
            con.execute("ALTER TABLE sale_items ADD COLUMN category_name TEXT NOT NULL DEFAULT 'Sem categoria'")
            con.execute("UPDATE sale_items SET category_name=COALESCE((SELECT c.name FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.id=sale_items.product_id),'Sem categoria')")
        # Small, neutral starter category; no fake products or sales are inserted.
        con.execute("INSERT OR IGNORE INTO categories(name,color,created_at) VALUES(?,?,?)", ("Sem categoria", "#94a3b8", now_iso()))

def rowdict(row): return dict(row) if row is not None else None
def rowsdict(rows): return [dict(r) for r in rows]
def audit(con, user_id, action, entity, entity_id=None, details=""):
    con.execute("INSERT INTO audit_log(user_id,action,entity,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, action, entity, entity_id, str(details)[:1000], now_iso()))
def safe_int(value, default=None, min_value=None, max_value=None):
    if value in (None, ""): return default
    if isinstance(value, bool): raise ValueError("Identificador inválido.")
    if isinstance(value, float) and not value.is_integer(): raise ValueError("Identificador inválido.")
    try: v = int(value)
    except (ValueError, TypeError): raise ValueError("Identificador inválido.")
    if min_value is not None and v < min_value: raise ValueError("Valor abaixo do limite permitido.")
    if max_value is not None and v > max_value: raise ValueError("Valor acima do limite permitido.")
    return v
def required_text(value, label, limit=200):
    if not isinstance(value, str): raise ValueError(f"{label} é obrigatório.")
    result = value.strip()
    if not result: raise ValueError(f"{label} é obrigatório.")
    if len(result) > limit: raise ValueError(f"{label} ultrapassa {limit} caracteres.")
    return result
def optional_text(value, limit=500):
    if value is None: return ""
    if not isinstance(value, str): raise ValueError("Texto inválido.")
    return value.strip()[:limit]
def date_range(qs):
    start = (qs.get("from") or [date.today().replace(day=1).isoformat()])[0]
    end = (qs.get("to") or [today_iso()])[0]
    try:
        datetime.strptime(start, "%Y-%m-%d"); datetime.strptime(end, "%Y-%m-%d")
    except ValueError: raise ValueError("Período de datas inválido.")
    if start > end: raise ValueError("A data inicial deve ser anterior à data final.")
    return start, end

class AppHandler(BaseHTTPRequestHandler):
    server_version = "MercadoPro/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Keep request logs useful without exposing payloads/passwords.
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_bytes(self, status, data, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        if headers:
            for k, v in headers.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def json(self, status, obj, headers=None):
        self.send_bytes(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), headers=headers)
    def ok(self, data=None, status=200, message=None, headers=None):
        body = {"ok": True}
        if data is not None: body["data"] = data
        if message: body["message"] = message
        self.json(status, body, headers=headers)
    def error(self, status, message): self.json(status, {"ok": False, "error": str(message)})

    def read_json(self):
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: raise ValueError("Tamanho de solicitação inválido.")
        if length > MAX_BODY: raise ValueError("Solicitação muito grande.")
        raw = self.rfile.read(length) if length else b"{}"
        try: data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError("JSON inválido.")
        if not isinstance(data, dict): raise ValueError("O corpo precisa ser um objeto JSON.")
        return data

    def session_user(self, con):
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "mercado_session": token = v
        if not token: return None, None
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = con.execute("SELECT s.csrf_token,s.expires_at,u.id,u.username,u.display_name,u.role,u.active FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (digest,)).fetchone()
        if not row or row["expires_at"] < int(time.time()) or not row["active"]:
            if row: con.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
            return None, None
        return row, digest

    def require_user(self, con, write=False):
        user, digest = self.session_user(con)
        if not user:
            self.error(401, "Sua sessão expirou. Entre novamente."); return None
        if write and self.command not in ("GET", "HEAD"):
            route = urlparse(self.path).path
            if user["role"] == "cashier" and route != "/api/sales/checkout":
                self.error(403, "Seu perfil não tem permissão para esta operação."); return None
            sent = self.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, user["csrf_token"]):
                self.error(403, "Validação de segurança falhou. Atualize a página e tente novamente."); return None
        return user

    def do_GET(self): self.route()
    def do_POST(self): self.route()
    def do_PUT(self): self.route()
    def do_PATCH(self): self.route()
    def do_DELETE(self): self.route()

    def route(self):
        parsed = urlparse(self.path); path = parsed.path; qs = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            try: self.send_bytes(200, (TEMPLATE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError: self.error(500, "Arquivo da interface não encontrado.")
            return
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            if "/" in name or ".." in name: self.error(404, "Arquivo não encontrado."); return
            target = STATIC_DIR / name
            types = {".css":"text/css; charset=utf-8", ".js":"text/javascript; charset=utf-8", ".svg":"image/svg+xml"}
            if not target.is_file(): self.error(404, "Arquivo não encontrado."); return
            self.send_bytes(200, target.read_bytes(), types.get(target.suffix, "application/octet-stream")); return
        if not path.startswith("/api/"):
            self.error(404, "Rota não encontrada."); return
        try:
            with connect_db() as con:
                if path == "/api/bootstrap" and self.command == "GET":
                    user, _ = self.session_user(con)
                    exists = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
                    self.ok({"setup_required": not exists, "authenticated": bool(user), "user": ({"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"], "csrf_token": user["csrf_token"]} if user else None), "version":"1.0.0"}); return
                if path == "/api/setup" and self.command == "POST": self.setup_admin(con); return
                if path == "/api/login" and self.command == "POST": self.login(con); return
                if path == "/api/logout" and self.command == "POST":
                    user, digest = self.session_user(con)
                    if digest:
                        with con: con.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                    self.ok(message="Sessão encerrada.", headers={"Set-Cookie":"mercado_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"}); return
                user = self.require_user(con, write=self.command != "GET")
                if not user: return
                self.api(con, user, path, qs)
        except ValueError as e: self.error(400, str(e))
        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "unique constraint failed: products.sku" in msg: self.error(409, "Já existe um produto com esse SKU.")
            elif "idx_products_barcode" in msg or "products.barcode" in msg: self.error(409, "Já existe um produto com esse código de barras.")
            elif "categories.name" in msg: self.error(409, "Já existe uma categoria com esse nome.")
            else: self.error(409, "A operação conflita com dados existentes ou referências vinculadas.")
        except Exception:
            traceback.print_exc()
            self.error(500, "Erro interno. Os dados não foram confirmados; confira o histórico e tente novamente.")

    def setup_admin(self, con):
        if con.execute("SELECT 1 FROM users LIMIT 1").fetchone(): self.error(409, "A configuração inicial já foi concluída."); return
        d = self.read_json(); username = required_text(d.get("username"), "Usuário", 40)
        if len(username) < 3 or any(not (c.isalnum() or c in "._-") for c in username): raise ValueError("Use ao menos 3 caracteres no usuário: letras, números, ponto, hífen ou sublinhado.")
        display = required_text(d.get("display_name"), "Nome", 80)
        password_hash = hash_password(d.get("password"))
        with con:
            cur = con.execute("INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES(?,?,?,'admin',?)", (username,display,password_hash,now_iso()))
            uid = cur.lastrowid
            token = secrets.token_urlsafe(40); csrf = secrets.token_urlsafe(32)
            con.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),uid,csrf,int(time.time())+SESSION_SECONDS,now_iso()))
            audit(con,uid,"setup","users",uid,"Conta administradora inicial criada")
        self.ok({"user":{"id":uid,"username":username,"display_name":display,"role":"admin","csrf_token":csrf}}, status=201, message="Conta administradora criada.", headers={"Set-Cookie":f"mercado_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"})

    def login(self, con):
        d = self.read_json(); username = required_text(d.get("username"), "Usuário", 80)
        password = d.get("password")
        row = con.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
        if not row or not isinstance(password, str) or not verify_password(password, row["password_hash"]):
            self.error(401, "Usuário ou senha incorretos."); return
        token = secrets.token_urlsafe(40); csrf = secrets.token_urlsafe(32)
        with con:
            con.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
            con.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),row["id"],csrf,int(time.time())+SESSION_SECONDS,now_iso()))
            audit(con,row["id"],"login","users",row["id"],"Login efetuado")
        self.ok({"user":{"id":row["id"],"username":row["username"],"display_name":row["display_name"],"role":row["role"],"csrf_token":csrf}}, headers={"Set-Cookie":f"mercado_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"})

    def api(self, con, user, path, qs):
        method = self.command
        if path == "/api/me" and method == "GET": self.ok({"id":user["id"],"username":user["username"],"display_name":user["display_name"],"role":user["role"],"csrf_token":user["csrf_token"]}); return
        if path == "/api/dashboard" and method == "GET": self.dashboard(con, qs); return
        if path == "/api/products": self.products(con,user); return
        if path.startswith("/api/products/"): self.product_detail(con,user,path); return
        if path == "/api/categories": self.categories(con,user); return
        if path.startswith("/api/categories/"): self.category_detail(con,user,path); return
        if path == "/api/discounts": self.discounts(con,user); return
        if path.startswith("/api/discounts/"): self.discount_detail(con,user,path); return
        if path == "/api/stock": self.stock(con,user,qs); return
        if path == "/api/sales": self.sales(con,user,qs); return
        if path == "/api/sales/checkout" and method == "POST": self.checkout(con,user); return
        if path.startswith("/api/sales/") and path.endswith("/cancel") and method == "POST": self.cancel_sale(con,user,path); return
        if path == "/api/expenses": self.expenses(con,user); return
        if path.startswith("/api/expenses/"): self.expense_detail(con,user,path); return
        if path == "/api/suppliers": self.suppliers(con,user); return
        if path == "/api/customers": self.customers(con,user); return
        if path == "/api/purchases": self.purchases(con,user); return
        if path.startswith("/api/purchases/") and path.endswith("/receive") and method == "POST": self.receive_purchase(con,user,path); return
        if path == "/api/analytics" and method == "GET": self.analytics(con,qs); return
        if path == "/api/users": self.users(con,user); return
        if path == "/api/backup" and method == "GET": self.backup(con); return
        if path == "/api/export" and method == "GET": self.export_csv(con,qs); return
        if path == "/api/audit" and method == "GET":
            if user["role"] != "admin": self.error(403,"Apenas administradores podem consultar a auditoria."); return
            self.ok(rowsdict(con.execute("SELECT a.*,u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 200"))); return
        self.error(404,"Rota não encontrada.")

    def products(self, con, user):
        m=self.command
        if m=="GET":
            q=parse_qs(urlparse(self.path).query).get("q",[""])[0].strip()
            rows=con.execute("SELECT p.*,c.name category_name,c.color category_color FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1 AND (?='' OR p.name LIKE ? OR p.sku LIKE ? OR p.barcode LIKE ?) ORDER BY p.name COLLATE NOCASE LIMIT 1000",(q,f"%{q}%",f"%{q}%",f"%{q}%")).fetchall()
            out=[]
            for r in rows:
                d=dict(r); d["cost"] = cents_to_money(d.pop("cost_cents")); d["price"] = cents_to_money(d.pop("price_cents")); d["stock"] = milli_to_qty(d.pop("stock_milli")); d["min_stock"] = milli_to_qty(d.pop("min_stock_milli")); d["effective_discount_bp"] = self.get_discount_bp(con,r["id"],r["category_id"]); d["sale_price"] = cents_to_money(max(0,r["price_cents"] - (r["price_cents"]*d["effective_discount_bp"]+5000)//10000)); out.append(d)
            self.ok(out);return
        if m=="POST":
            d=self.read_json(); name=required_text(d.get("name"),"Nome do produto",150); sku=required_text(d.get("sku"),"SKU",60)
            category=safe_int(d.get("category_id"),None,1); unit=required_text(d.get("unit","un"),"Unidade",10)
            cost=money_to_cents(d.get("cost",0)); price=money_to_cents(d.get("price",0)); stock=qty_to_milli(d.get("stock",0),True); minimum=qty_to_milli(d.get("min_stock",0),True)
            validate_count_unit(unit, stock, "estoque inicial"); validate_count_unit(unit, minimum, "estoque mínimo")
            barcode=optional_text(d.get("barcode"),80); desc=optional_text(d.get("description"),1000)
            if category and not con.execute("SELECT 1 FROM categories WHERE id=?",(category,)).fetchone(): raise ValueError("Categoria não encontrada.")
            with con:
                cur=con.execute("INSERT INTO products(sku,barcode,name,description,category_id,unit,cost_cents,price_cents,stock_milli,min_stock_milli,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(sku,barcode,name,desc,category,unit,cost,price,stock,minimum,now_iso(),now_iso()))
                pid=cur.lastrowid
                if stock>0: con.execute("INSERT INTO stock_movements(product_id,kind,quantity_milli,before_milli,after_milli,unit_cost_cents,note,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(pid,"entry",stock,0,stock,cost,"Estoque inicial",user["id"],now_iso()))
                audit(con,user["id"],"create","product",pid,name)
            self.ok({"id":pid},201,"Produto cadastrado.");return
        self.error(405,"Método não permitido.")

    def product_detail(self,con,user,path):
        try: pid=int(path.rsplit("/",1)[1])
        except ValueError: self.error(404,"Produto não encontrado.");return
        m=self.command; row=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if not row: self.error(404,"Produto não encontrado.");return
        if m in ("PUT","PATCH"):
            d=self.read_json(); name=required_text(d.get("name",row["name"]),"Nome do produto",150); sku=required_text(d.get("sku",row["sku"]),"SKU",60); barcode=optional_text(d.get("barcode",row["barcode"]),80); desc=optional_text(d.get("description",row["description"]),1000)
            category=safe_int(d.get("category_id",row["category_id"]),None,1); unit=required_text(d.get("unit",row["unit"]),"Unidade",10)
            cost=money_to_cents(d.get("cost",cents_to_money(row["cost_cents"]))); price=money_to_cents(d.get("price",cents_to_money(row["price_cents"]))); minimum=qty_to_milli(d.get("min_stock",milli_to_qty(row["min_stock_milli"])),True)
            validate_count_unit(unit, minimum, "estoque mínimo")
            validate_count_unit(unit, row["stock_milli"], "estoque atual")
            if category and not con.execute("SELECT 1 FROM categories WHERE id=?",(category,)).fetchone(): raise ValueError("Categoria não encontrada.")
            with con:
                con.execute("UPDATE products SET sku=?,barcode=?,name=?,description=?,category_id=?,unit=?,cost_cents=?,price_cents=?,min_stock_milli=?,updated_at=? WHERE id=?",(sku,barcode,name,desc,category,unit,cost,price,minimum,now_iso(),pid))
                audit(con,user["id"],"update","product",pid,name)
            self.ok(message="Produto atualizado.");return
        if m=="DELETE":
            if row["stock_milli"]>0: self.error(409,"Este produto ainda tem estoque. Zere o estoque antes de arquivá-lo.");return
            with con: con.execute("UPDATE products SET active=0,updated_at=? WHERE id=?",(now_iso(),pid)); con.execute("UPDATE discounts SET active=0 WHERE target_type='product' AND target_id=?",(pid,)); audit(con,user["id"],"archive","product",pid,row["name"])
            self.ok(message="Produto arquivado.");return
        self.error(405,"Método não permitido.")

    def categories(self,con,user):
        if self.command=="GET": self.ok(rowsdict(con.execute("SELECT c.*,COUNT(p.id) product_count FROM categories c LEFT JOIN products p ON p.category_id=c.id AND p.active=1 GROUP BY c.id ORDER BY c.name COLLATE NOCASE")));return
        if self.command=="POST":
            d=self.read_json(); name=required_text(d.get("name"),"Nome da categoria",80); color=optional_text(d.get("color","#32d583"),20) or "#32d583"
            if not (color.startswith("#") and len(color) in (4,7) and all(c in "#0123456789abcdefABCDEF" for c in color)): raise ValueError("Cor inválida.")
            with con: cur=con.execute("INSERT INTO categories(name,color,created_at) VALUES(?,?,?)",(name,color,now_iso())); audit(con,user["id"],"create","category",cur.lastrowid,name)
            self.ok({"id":cur.lastrowid},201,"Categoria criada.");return
        self.error(405,"Método não permitido.")
    def category_detail(self,con,user,path):
        try: cid=int(path.rsplit("/",1)[1])
        except ValueError: self.error(404,"Categoria não encontrada.");return
        row=con.execute("SELECT * FROM categories WHERE id=?",(cid,)).fetchone()
        if not row: self.error(404,"Categoria não encontrada.");return
        if self.command in ("PUT","PATCH"):
            d=self.read_json(); name=required_text(d.get("name",row["name"]),"Nome da categoria",80); color=optional_text(d.get("color",row["color"]),20) or row["color"]
            if not (color.startswith("#") and len(color) in (4,7) and all(c in "#0123456789abcdefABCDEF" for c in color)): raise ValueError("Cor inválida.")
            with con: con.execute("UPDATE categories SET name=?,color=? WHERE id=?",(name,color,cid)); audit(con,user["id"],"update","category",cid,name)
            self.ok(message="Categoria atualizada.");return
        if self.command=="DELETE":
            if con.execute("SELECT 1 FROM products WHERE category_id=? AND active=1 LIMIT 1",(cid,)).fetchone(): self.error(409,"A categoria possui produtos. Reatribua os produtos antes de excluir.");return
            if row["name"]=="Sem categoria": self.error(409,"A categoria padrão não pode ser excluída.");return
            with con: con.execute("DELETE FROM categories WHERE id=?",(cid,)); con.execute("DELETE FROM discounts WHERE target_type='category' AND target_id=?",(cid,)); audit(con,user["id"],"delete","category",cid,row["name"])
            self.ok(message="Categoria excluída.");return
        self.error(405,"Método não permitido.")

    def discounts(self,con,user):
        if self.command=="GET":
            self.ok(rowsdict(con.execute("SELECT d.*,CASE WHEN d.target_type='product' THEN p.name ELSE c.name END target_name FROM discounts d LEFT JOIN products p ON d.target_type='product' AND p.id=d.target_id LEFT JOIN categories c ON d.target_type='category' AND c.id=d.target_id ORDER BY d.active DESC,d.id DESC")));return
        if self.command=="POST":
            d=self.read_json(); name=required_text(d.get("name"),"Nome da promoção",100); typ=d.get("target_type")
            if typ not in ("product","category"): raise ValueError("Escolha produto ou categoria.")
            tid=safe_int(d.get("target_id"),None,1); bp=pct_to_bp(d.get("percent"))
            if not tid: raise ValueError("Selecione um produto ou uma categoria.")
            exists=con.execute("SELECT 1 FROM products WHERE id=? AND active=1",(tid,)).fetchone() if typ=="product" else con.execute("SELECT 1 FROM categories WHERE id=?",(tid,)).fetchone()
            if not exists: raise ValueError("O item selecionado não existe.")
            start=optional_text(d.get("starts_at",""),10); end=optional_text(d.get("ends_at",""),10)
            for x in (start,end):
                if x: datetime.strptime(x,"%Y-%m-%d")
            if start and end and start>end: raise ValueError("A data final deve ser igual ou posterior à inicial.")
            with con: cur=con.execute("INSERT INTO discounts(name,target_type,target_id,percent_bp,starts_at,ends_at,created_at) VALUES(?,?,?,?,?,?,?)",(name,typ,tid,bp,start,end,now_iso())); audit(con,user["id"],"create","discount",cur.lastrowid,name)
            self.ok({"id":cur.lastrowid},201,"Promoção criada.");return
        self.error(405,"Método não permitido.")
    def discount_detail(self,con,user,path):
        try: did=int(path.rsplit("/",1)[1])
        except ValueError: self.error(404,"Promoção não encontrada.");return
        row=con.execute("SELECT * FROM discounts WHERE id=?",(did,)).fetchone()
        if not row: self.error(404,"Promoção não encontrada.");return
        if self.command in ("PATCH","PUT"):
            d=self.read_json(); active=bool(d.get("active",not row["active"]))
            with con: con.execute("UPDATE discounts SET active=? WHERE id=?",(int(active),did)); audit(con,user["id"],"toggle","discount",did,str(active))
            self.ok(message="Status da promoção atualizado.");return
        if self.command=="DELETE":
            with con: con.execute("DELETE FROM discounts WHERE id=?",(did,)); audit(con,user["id"],"delete","discount",did,row["name"])
            self.ok(message="Promoção excluída.");return
        self.error(405,"Método não permitido.")

    def get_discount_bp(self,con,product_id,category_id,at_date=None):
        at_date=at_date or today_iso()
        rows=con.execute("SELECT percent_bp FROM discounts WHERE active=1 AND (starts_at='' OR starts_at<=?) AND (ends_at='' OR ends_at>=?) AND ((target_type='product' AND target_id=?) OR (target_type='category' AND target_id=?))",(at_date,at_date,product_id,category_id or -1)).fetchall()
        # Promotions are not stacked; the strongest valid offer wins.
        return max([r["percent_bp"] for r in rows],default=0)

    def stock(self,con,user,qs):
        if self.command=="GET":
            q=(qs.get("q") or [""])[0].strip(); low=(qs.get("low") or ["0"])[0]=="1"
            sql="SELECT p.id,p.sku,p.name,p.unit,p.stock_milli,p.min_stock_milli,p.cost_cents,c.name category_name FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1 AND (?='' OR p.name LIKE ? OR p.sku LIKE ?)"
            args=[q,f"%{q}%",f"%{q}%"]
            if low: sql += " AND p.stock_milli<=p.min_stock_milli"
            products=rowsdict(con.execute(sql+" ORDER BY (p.stock_milli<=p.min_stock_milli) DESC,p.name COLLATE NOCASE",args))
            for p in products: p["stock"]=milli_to_qty(p.pop("stock_milli")); p["min_stock"]=milli_to_qty(p.pop("min_stock_milli")); p["unit_cost"]=cents_to_money(p.pop("cost_cents"))
            hist=rowsdict(con.execute("SELECT m.*,p.sku,p.name product_name,u.username FROM stock_movements m JOIN products p ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT 150"))
            for x in hist:
                for k in ("quantity_milli","before_milli","after_milli"): x[k]=milli_to_qty(x[k])
                x["unit_cost"]=cents_to_money(x.pop("unit_cost_cents"))
            self.ok({"products":products,"movements":hist});return
        if self.command=="POST":
            d=self.read_json(); pid=safe_int(d.get("product_id"),None,1); kind=d.get("kind","entry")
            if kind not in ("entry","adjustment","return"): raise ValueError("Tipo de movimentação inválido.")
            qty=qty_to_milli(d.get("quantity"),True)
            if qty==0 and kind!="adjustment": raise ValueError("A quantidade precisa ser maior que zero.")
            note=required_text(d.get("note", "Movimentação manual"),"Motivo da movimentação",200)
            p=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if not p: raise ValueError("Produto não encontrado.")
            if kind=="adjustment": target=qty; delta=target-p["stock_milli"]
            else: delta=qty; target=p["stock_milli"]+delta
            validate_count_unit(p["unit"], target, "estoque final")
            validate_count_unit(p["unit"], abs(delta), "quantidade movimentada")
            with con:
                if target<0: raise ValueError("A operação deixaria o estoque negativo.")
                con.execute("UPDATE products SET stock_milli=?,updated_at=? WHERE id=?",(target,now_iso(),pid))
                con.execute("INSERT INTO stock_movements(product_id,kind,quantity_milli,before_milli,after_milli,unit_cost_cents,note,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(pid,kind,delta,p["stock_milli"],target,p["cost_cents"],note,user["id"],now_iso()))
                audit(con,user["id"],"stock_"+kind,"product",pid,f"{p['sku']} {delta}")
            self.ok({"stock":milli_to_qty(target)},message="Estoque atualizado.");return
        self.error(405,"Método não permitido.")

    def checkout(self,con,user):
        d=self.read_json(); cart=d.get("items")
        if not isinstance(cart,list) or not cart or len(cart)>200: raise ValueError("A venda deve conter entre 1 e 200 itens.")
        payment=d.get("payment_method")
        if payment not in ("dinheiro","pix","credito","debito","vale","outro"): raise ValueError("Forma de pagamento inválida.")
        customer_id=safe_int(d.get("customer_id"),None,1); customer_name=optional_text(d.get("customer_name",""),150)
        notes=optional_text(d.get("notes",""),500)
        if customer_id and not con.execute("SELECT 1 FROM customers WHERE id=?",(customer_id,)).fetchone(): raise ValueError("Cliente não encontrado.")
        if customer_id: customer_name=con.execute("SELECT name FROM customers WHERE id=?",(customer_id,)).fetchone()[0]
        with con:
            items=[]; subtotal=discount_total=cost_total=total=0; seen={}
            for it in cart:
                pid=safe_int(it.get("product_id"),None,1); qty=qty_to_milli(it.get("quantity"))
                seen[pid]=seen.get(pid,0)+qty
            for pid, qty in seen.items():
                p=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
                if not p: raise ValueError(f"Um produto da venda não está disponível (ID {pid}).")
                validate_count_unit(p["unit"], qty, "quantidade vendida")
                if p["stock_milli"] < qty: raise ValueError(f"Estoque insuficiente para {p['name']}. Disponível: {milli_to_qty(p['stock_milli']):g} {p['unit']}.")
                bp=self.get_discount_bp(con,pid,p["category_id"])
                sub=mul_money(p["price_cents"],qty); off=(sub*bp+5000)//10000; line_total=sub-off; cost=mul_money(p["cost_cents"],qty)
                subtotal+=sub; discount_total+=off; total+=line_total; cost_total+=cost
                items.append({"p":p,"qty":qty,"bp":bp,"sub":sub,"off":off,"total":line_total,"cost":cost})
            code="V"+datetime.now().strftime("%y%m%d%H%M%S")+secrets.token_hex(2).upper()
            cur=con.execute("INSERT INTO sales(code,customer_id,customer_name,payment_method,subtotal_cents,discount_cents,total_cents,cost_cents,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(code,customer_id,customer_name,payment,subtotal,discount_total,total,cost_total,notes,user["id"],now_iso()))
            sale_id=cur.lastrowid
            for x in items:
                p=x["p"]; qty=x["qty"]
                category_name = con.execute("SELECT name FROM categories WHERE id=?", (p["category_id"],)).fetchone()[0] if p["category_id"] else "Sem categoria"
                con.execute("INSERT INTO sale_items(sale_id,product_id,sku,product_name,category_name,quantity_milli,unit_price_cents,discount_bp,line_subtotal_cents,line_discount_cents,line_total_cents,unit_cost_cents,line_cost_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(sale_id,p["id"],p["sku"],p["name"],category_name,qty,p["price_cents"],x["bp"],x["sub"],x["off"],x["total"],p["cost_cents"],x["cost"]))
                after=p["stock_milli"]-qty
                con.execute("UPDATE products SET stock_milli=?,updated_at=? WHERE id=?",(after,now_iso(),p["id"]))
                con.execute("INSERT INTO stock_movements(product_id,kind,quantity_milli,before_milli,after_milli,unit_cost_cents,note,reference_type,reference_id,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(p["id"],"sale",-qty,p["stock_milli"],after,p["cost_cents"],f"Venda {code}","sale",sale_id,user["id"],now_iso()))
            audit(con,user["id"],"checkout","sale",sale_id,code)
        self.ok({"id":sale_id,"code":code,"subtotal":cents_to_money(subtotal),"discount":cents_to_money(discount_total),"total":cents_to_money(total),"cost":cents_to_money(cost_total)},201,"Venda registrada e estoque atualizado.")

    def sales(self,con,user,qs):
        m=self.command
        if m=="GET":
            start,end=date_range(qs); rows=rowsdict(con.execute("SELECT s.*,u.display_name cashier FROM sales s LEFT JOIN users u ON u.id=s.created_by WHERE substr(s.created_at,1,10) BETWEEN ? AND ? ORDER BY s.id DESC LIMIT 1000",(start,end)))
            for r in rows:
                for k in ("subtotal_cents","discount_cents","total_cents","cost_cents"): r[k.removesuffix("_cents")]=cents_to_money(r.pop(k))
                r["items"]=rowsdict(con.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id",(r["id"],)))
                for i in r["items"]:
                    i["quantity"]=milli_to_qty(i.pop("quantity_milli"));
                    for k in ("unit_price_cents","line_subtotal_cents","line_discount_cents","line_total_cents","unit_cost_cents","line_cost_cents"): i[k.removesuffix("_cents")]=cents_to_money(i.pop(k))
                    i["discount_percent"]=bp_to_pct(i.pop("discount_bp"))
            self.ok(rows);return
        self.error(405,"Método não permitido.")
    def cancel_sale(self,con,user,path):
        if user["role"]=="cashier": self.error(403,"Seu perfil não pode cancelar vendas.");return
        try: sid=int(path.split("/")[-2])
        except ValueError: self.error(404,"Venda não encontrada.");return
        sale=con.execute("SELECT * FROM sales WHERE id=?",(sid,)).fetchone()
        if not sale: self.error(404,"Venda não encontrada.");return
        if sale["status"]!="completed": self.error(409,"Esta venda já foi cancelada.");return
        d=self.read_json(); reason=required_text(d.get("reason"),"Motivo do cancelamento",250)
        with con:
            lines=con.execute("SELECT * FROM sale_items WHERE sale_id=?",(sid,)).fetchall()
            for line in lines:
                if line["product_id"] is None: continue
                p=con.execute("SELECT * FROM products WHERE id=?",(line["product_id"],)).fetchone()
                if not p: continue
                after=p["stock_milli"]+line["quantity_milli"]
                con.execute("UPDATE products SET stock_milli=?,updated_at=? WHERE id=?",(after,now_iso(),p["id"]))
                con.execute("INSERT INTO stock_movements(product_id,kind,quantity_milli,before_milli,after_milli,unit_cost_cents,note,reference_type,reference_id,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(p["id"],"cancellation",line["quantity_milli"],p["stock_milli"],after,line["unit_cost_cents"],f"Estorno {sale['code']}: {reason}","sale",sid,user["id"],now_iso()))
            con.execute("UPDATE sales SET status='cancelled',cancelled_at=?,notes=CASE WHEN notes='' THEN ? ELSE notes||' | Cancelamento: '||? END WHERE id=?",(now_iso(),reason,reason,sid))
            audit(con,user["id"],"cancel","sale",sid,reason)
        self.ok(message="Venda cancelada e estoque revertido.")

    def expenses(self,con,user):
        if self.command=="GET":
            qs=parse_qs(urlparse(self.path).query); start,end=date_range(qs)
            rows=rowsdict(con.execute("SELECT e.*,u.display_name created_by_name FROM expenses e LEFT JOIN users u ON u.id=e.created_by WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date DESC,id DESC",(start,end)))
            for r in rows:r["amount"]=cents_to_money(r.pop("amount_cents"))
            self.ok(rows);return
        if self.command=="POST":
            d=self.read_json(); category=required_text(d.get("category"),"Categoria da despesa",80); desc=required_text(d.get("description"),"Descrição",180); amount=money_to_cents(d.get("amount")); exdate=optional_text(d.get("expense_date",today_iso()),10); datetime.strptime(exdate,"%Y-%m-%d")
            if amount<=0: raise ValueError("A despesa deve ser maior que zero.")
            pay=optional_text(d.get("payment_method","outro"),30); notes=optional_text(d.get("notes",""),500)
            with con: cur=con.execute("INSERT INTO expenses(category,description,amount_cents,expense_date,payment_method,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",(category,desc,amount,exdate,pay,notes,user["id"],now_iso())); audit(con,user["id"],"create","expense",cur.lastrowid,desc)
            self.ok({"id":cur.lastrowid},201,"Despesa registrada.");return
        self.error(405,"Método não permitido.")
    def expense_detail(self,con,user,path):
        try:eid=int(path.rsplit("/",1)[1])
        except ValueError:self.error(404,"Despesa não encontrada.");return
        row=con.execute("SELECT * FROM expenses WHERE id=?",(eid,)).fetchone()
        if not row:self.error(404,"Despesa não encontrada.");return
        if self.command=="DELETE":
            if user["role"]=="cashier":self.error(403,"Seu perfil não pode excluir despesas.");return
            with con:con.execute("DELETE FROM expenses WHERE id=?",(eid,));audit(con,user["id"],"delete","expense",eid,row["description"])
            self.ok(message="Despesa removida.");return
        self.error(405,"Método não permitido.")

    def suppliers(self,con,user):
        if self.command=="GET":self.ok(rowsdict(con.execute("SELECT * FROM suppliers WHERE active=1 ORDER BY name COLLATE NOCASE")));return
        if self.command=="POST":
            d=self.read_json(); name=required_text(d.get("name"),"Nome do fornecedor",150)
            vals=(name,optional_text(d.get("document",""),30),optional_text(d.get("contact",""),120),optional_text(d.get("email",""),150),optional_text(d.get("phone",""),40),optional_text(d.get("notes",""),500),now_iso())
            with con:cur=con.execute("INSERT INTO suppliers(name,document,contact,email,phone,notes,created_at) VALUES(?,?,?,?,?,?,?)",vals);audit(con,user["id"],"create","supplier",cur.lastrowid,name)
            self.ok({"id":cur.lastrowid},201,"Fornecedor cadastrado.");return
        self.error(405,"Método não permitido.")

    def customers(self,con,user):
        if self.command=="GET":
            q=parse_qs(urlparse(self.path).query).get("q",[""])[0].strip()
            self.ok(rowsdict(con.execute("SELECT * FROM customers WHERE (?='' OR name LIKE ? OR phone LIKE ? OR document LIKE ?) ORDER BY name COLLATE NOCASE LIMIT 1000",(q,f"%{q}%",f"%{q}%",f"%{q}%"))));return
        if self.command=="POST":
            d=self.read_json(); name=required_text(d.get("name"),"Nome do cliente",150)
            with con:cur=con.execute("INSERT INTO customers(name,phone,email,document,notes,created_at) VALUES(?,?,?,?,?,?)",(name,optional_text(d.get("phone",""),40),optional_text(d.get("email",""),150),optional_text(d.get("document",""),30),optional_text(d.get("notes",""),500),now_iso()));audit(con,user["id"],"create","customer",cur.lastrowid,name)
            self.ok({"id":cur.lastrowid},201,"Cliente cadastrado.");return
        self.error(405,"Método não permitido.")

    def purchases(self,con,user):
        if self.command=="GET":
            rows=rowsdict(con.execute("SELECT o.*,s.name supplier_name,u.display_name created_by_name FROM purchase_orders o LEFT JOIN suppliers s ON s.id=o.supplier_id LEFT JOIN users u ON u.id=o.created_by ORDER BY o.id DESC LIMIT 500"))
            for r in rows:
                r["total"]=cents_to_money(r.pop("total_cents"));r["items"]=rowsdict(con.execute("SELECT i.*,p.name product_name,p.sku,p.unit FROM purchase_order_items i JOIN products p ON p.id=i.product_id WHERE purchase_order_id=? ORDER BY i.id",(r["id"],)))
                for x in r["items"]:x["quantity"]=milli_to_qty(x.pop("quantity_milli"));x["unit_cost"]=cents_to_money(x.pop("unit_cost_cents"));x["line_total"]=cents_to_money(x.pop("line_total_cents"))
            self.ok(rows);return
        if self.command=="POST":
            d=self.read_json(); supplier=safe_int(d.get("supplier_id"),None,1); items=d.get("items")
            if not isinstance(items,list) or not items or len(items)>200:raise ValueError("O pedido deve conter entre 1 e 200 itens.")
            if supplier and not con.execute("SELECT 1 FROM suppliers WHERE id=? AND active=1",(supplier,)).fetchone():raise ValueError("Fornecedor não encontrado.")
            parsed=[]; seen=set(); total=0
            for it in items:
                pid=safe_int(it.get("product_id"),None,1); qty=qty_to_milli(it.get("quantity"));cost=money_to_cents(it.get("unit_cost",0))
                if pid in seen:raise ValueError("Não repita o mesmo produto no pedido.")
                seen.add(pid);p=con.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
                if not p:raise ValueError("Produto do pedido não encontrado.")
                validate_count_unit(p["unit"], qty, "quantidade comprada")
                line=mul_money(cost,qty);total+=line;parsed.append((pid,qty,cost,line))
            code="C"+datetime.now().strftime("%y%m%d%H%M%S")+secrets.token_hex(2).upper()
            with con:
                cur=con.execute("INSERT INTO purchase_orders(code,supplier_id,status,total_cents,notes,created_by,created_at) VALUES(?,?,'draft',?,?,?,?)",(code,supplier,total,optional_text(d.get("notes",""),500),user["id"],now_iso())); oid=cur.lastrowid
                for pid,qty,cost,line in parsed:con.execute("INSERT INTO purchase_order_items(purchase_order_id,product_id,quantity_milli,unit_cost_cents,line_total_cents) VALUES(?,?,?,?,?)",(oid,pid,qty,cost,line))
                audit(con,user["id"],"create","purchase",oid,code)
            self.ok({"id":oid,"code":code,"total":cents_to_money(total)},201,"Pedido de compra criado como rascunho.");return
        self.error(405,"Método não permitido.")
    def receive_purchase(self,con,user,path):
        if user["role"]=="cashier":self.error(403,"Seu perfil não pode receber compras.");return
        try: oid=int(path.split("/")[-2])
        except ValueError:self.error(404,"Pedido não encontrado.");return
        order=con.execute("SELECT * FROM purchase_orders WHERE id=?",(oid,)).fetchone()
        if not order:self.error(404,"Pedido não encontrado.");return
        if order["status"]!="draft":self.error(409,"Somente pedidos em rascunho podem ser recebidos.");return
        with con:
            items=con.execute("SELECT * FROM purchase_order_items WHERE purchase_order_id=?",(oid,)).fetchall()
            for i in items:
                p=con.execute("SELECT * FROM products WHERE id=?",(i["product_id"],)).fetchone()
                if not p:raise ValueError("Um produto do pedido não existe mais.")
                after=p["stock_milli"]+i["quantity_milli"]
                # Receiving a purchase updates both quantity and the latest unit cost.
                con.execute("UPDATE products SET stock_milli=?,cost_cents=?,updated_at=? WHERE id=?",(after,i["unit_cost_cents"],now_iso(),p["id"]))
                con.execute("INSERT INTO stock_movements(product_id,kind,quantity_milli,before_milli,after_milli,unit_cost_cents,note,reference_type,reference_id,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(p["id"],"purchase",i["quantity_milli"],p["stock_milli"],after,i["unit_cost_cents"],f"Recebimento {order['code']}","purchase",oid,user["id"],now_iso()))
            con.execute("UPDATE purchase_orders SET status='received',received_at=? WHERE id=?",(now_iso(),oid));audit(con,user["id"],"receive","purchase",oid,order["code"])
        self.ok(message="Compra recebida. Estoque e custo atualizados.")

    def dashboard(self,con,qs):
        start,end=date_range(qs)
        sales=con.execute("SELECT COUNT(*) n,COALESCE(SUM(total_cents),0) revenue,COALESCE(SUM(discount_cents),0) discounts,COALESCE(SUM(cost_cents),0) cost FROM sales WHERE status='completed' AND substr(created_at,1,10) BETWEEN ? AND ?",(start,end)).fetchone()
        expenses=con.execute("SELECT COALESCE(SUM(amount_cents),0) amount FROM expenses WHERE expense_date BETWEEN ? AND ?",(start,end)).fetchone()["amount"]
        low=rowsdict(con.execute("SELECT p.id,p.sku,p.name,p.unit,p.stock_milli,p.min_stock_milli,c.name category_name FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1 AND p.stock_milli<=p.min_stock_milli ORDER BY (p.min_stock_milli-p.stock_milli) DESC,p.name LIMIT 8"))
        for p in low:p["stock"]=milli_to_qty(p.pop("stock_milli"));p["min_stock"]=milli_to_qty(p.pop("min_stock_milli"))
        top=rowsdict(con.execute("SELECT si.product_id,si.product_name,SUM(si.quantity_milli) quantity_milli,SUM(si.line_total_cents) revenue FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.status='completed' AND substr(s.created_at,1,10) BETWEEN ? AND ? GROUP BY si.product_id,si.product_name ORDER BY quantity_milli DESC LIMIT 5",(start,end)))
        bottom=rowsdict(con.execute("SELECT p.id product_id,p.name product_name,COALESCE(SUM(CASE WHEN s.status='completed' THEN si.quantity_milli ELSE 0 END),0) quantity_milli FROM products p LEFT JOIN sale_items si ON si.product_id=p.id LEFT JOIN sales s ON s.id=si.sale_id AND substr(s.created_at,1,10) BETWEEN ? AND ? WHERE p.active=1 GROUP BY p.id ORDER BY quantity_milli ASC,p.name LIMIT 5",(start,end)))
        trend=rowsdict(con.execute("SELECT substr(created_at,1,10) day,SUM(total_cents) revenue,COUNT(*) orders FROM sales WHERE status='completed' AND substr(created_at,1,10) BETWEEN ? AND ? GROUP BY substr(created_at,1,10) ORDER BY day",(start,end)))
        recent=rowsdict(con.execute("SELECT id,code,customer_name,payment_method,total_cents,status,created_at FROM sales ORDER BY id DESC LIMIT 6"))
        for r in recent:r["total"]=cents_to_money(r.pop("total_cents"))
        self.ok({"period":{"from":start,"to":end},"sales_count":sales["n"],"revenue":cents_to_money(sales["revenue"]),"discounts":cents_to_money(sales["discounts"]),"cost":cents_to_money(sales["cost"]),"expenses":cents_to_money(expenses),"gross_profit":cents_to_money(sales["revenue"]-sales["cost"]),"estimated_result":cents_to_money(sales["revenue"]-sales["cost"]-expenses),"low_stock":low,"top_products":[{**r,"quantity":milli_to_qty(r.pop("quantity_milli")),"revenue":cents_to_money(r.pop("revenue"))} for r in top],"slow_products":[{**r,"quantity":milli_to_qty(r.pop("quantity_milli"))} for r in bottom],"trend":[{**r,"revenue":cents_to_money(r.pop("revenue"))} for r in trend],"recent_sales":recent,"products_count":con.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],"categories_count":con.execute("SELECT COUNT(*) FROM categories").fetchone()[0],"stock_value":cents_to_money(con.execute("SELECT COALESCE(SUM(stock_milli*cost_cents)/1000,0) FROM products WHERE active=1").fetchone()[0]),"active_discounts":con.execute("SELECT COUNT(*) FROM discounts WHERE active=1").fetchone()[0]})

    def analytics(self,con,qs):
        start,end=date_range(qs)
        base=con.execute("SELECT COUNT(*) n,COALESCE(SUM(total_cents),0) revenue,COALESCE(SUM(discount_cents),0) discounts,COALESCE(SUM(cost_cents),0) cost FROM sales WHERE status='completed' AND substr(created_at,1,10) BETWEEN ? AND ?",(start,end)).fetchone()
        expense=con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE expense_date BETWEEN ? AND ?",(start,end)).fetchone()[0]
        bycat=rowsdict(con.execute("SELECT COALESCE(NULLIF(si.category_name,''),'Sem categoria') category,COALESCE(SUM(si.line_total_cents),0) revenue,COALESCE(SUM(si.line_cost_cents),0) cost,COALESCE(SUM(si.quantity_milli),0) quantity_milli FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.status='completed' AND substr(s.created_at,1,10) BETWEEN ? AND ? GROUP BY category ORDER BY revenue DESC",(start,end)))
        for r in bycat:r["revenue"]=cents_to_money(r["revenue"]);r["cost"]=cents_to_money(r["cost"]);r["quantity"]=milli_to_qty(r.pop("quantity_milli"))
        payments=rowsdict(con.execute("SELECT payment_method,COUNT(*) count,SUM(total_cents) total FROM sales WHERE status='completed' AND substr(created_at,1,10) BETWEEN ? AND ? GROUP BY payment_method ORDER BY total DESC",(start,end)))
        for r in payments:r["total"]=cents_to_money(r["total"])
        self.ok({"from":start,"to":end,"orders":base["n"],"revenue":cents_to_money(base["revenue"]),"discounts":cents_to_money(base["discounts"]),"cost":cents_to_money(base["cost"]),"gross_profit":cents_to_money(base["revenue"]-base["cost"]),"expenses":cents_to_money(expense),"estimated_result":cents_to_money(base["revenue"]-base["cost"]-expense),"margin_pct":round((base["revenue"]-base["cost"])/base["revenue"]*100,2) if base["revenue"] else 0,"by_category":bycat,"payments":payments})

    def users(self,con,user):
        if user["role"]!="admin":self.error(403,"Apenas administradores podem gerenciar usuários.");return
        if self.command=="GET":self.ok(rowsdict(con.execute("SELECT id,username,display_name,role,active,created_at FROM users ORDER BY id")));return
        if self.command=="POST":
            d=self.read_json(); username=required_text(d.get("username"),"Usuário",40); display=required_text(d.get("display_name"),"Nome",80); role=d.get("role")
            if len(username)<3 or any(not (c.isalnum() or c in "._-") for c in username):raise ValueError("Nome de usuário inválido.")
            if role not in ("admin","manager","cashier"):raise ValueError("Perfil inválido.")
            hp=hash_password(d.get("password"))
            with con:cur=con.execute("INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?)",(username,display,hp,role,now_iso()));audit(con,user["id"],"create","user",cur.lastrowid,username)
            self.ok({"id":cur.lastrowid},201,"Usuário criado.");return
        self.error(405,"Método não permitido.")

    def backup(self,con):
        # SQLite backup API creates a consistent, standalone snapshot even in WAL mode.
        DATA_DIR.mkdir(parents=True,exist_ok=True); target=io.BytesIO()
        dest=sqlite3.connect(":memory:")
        try: con.backup(dest); dest.commit(); dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally: pass
        # Use a temporary backup file because sqlite3.Connection.serialize is not available on all builds.
        dest_path=DATA_DIR/("backup-"+datetime.now().strftime("%Y%m%d-%H%M%S")+".sqlite3")
        out=sqlite3.connect(dest_path)
        try: dest.backup(out); out.commit()
        finally: out.close();dest.close()
        data=dest_path.read_bytes()
        try:dest_path.unlink()
        except OSError:pass
        self.send_bytes(200,data,"application/vnd.sqlite3",{"Content-Disposition":f'attachment; filename="mercado-backup-{date.today().isoformat()}.sqlite3"'})

    def export_csv(self,con,qs):
        kind=(qs.get("type") or ["products"])[0]
        tables={
          "products":("SELECT sku,barcode,name,description,category_id,unit,cost_cents,price_cents,stock_milli,min_stock_milli,active,created_at,updated_at FROM products ORDER BY name", ["SKU","Codigo de barras","Produto","Descricao","ID categoria","Unidade","Custo (centavos)","Preco (centavos)","Estoque (milesimos)","Estoque minimo (milesimos)","Ativo","Criado em","Atualizado em"]),
          "sales":("SELECT s.code,s.created_at,s.status,s.customer_name,s.payment_method,s.subtotal_cents,s.discount_cents,s.total_cents,s.cost_cents,si.sku,si.product_name,si.quantity_milli,si.unit_price_cents,si.line_discount_cents,si.line_total_cents FROM sales s LEFT JOIN sale_items si ON si.sale_id=s.id ORDER BY s.id,si.id",["Venda","Data","Status","Cliente","Pagamento","Subtotal centavos","Desconto centavos","Total centavos","Custo centavos","SKU","Produto","Quantidade milesimos","Preco unitario centavos","Desconto item centavos","Total item centavos"]),
          "stock":("SELECT m.created_at,p.sku,p.name,m.kind,m.quantity_milli,m.before_milli,m.after_milli,m.unit_cost_cents,m.note,m.reference_type,m.reference_id FROM stock_movements m JOIN products p ON p.id=m.product_id ORDER BY m.id DESC",["Data","SKU","Produto","Movimento","Quantidade milesimos","Antes milesimos","Depois milesimos","Custo centavos","Motivo","Referencia","ID referencia"]),
          "expenses":("SELECT expense_date,category,description,amount_cents,payment_method,notes,created_at FROM expenses ORDER BY expense_date DESC",["Data","Categoria","Descricao","Valor em centavos","Pagamento","Observacoes","Criado em"])}
        if kind not in tables:raise ValueError("Tipo de exportação inválido.")
        sql,headers=tables[kind]; buf=io.StringIO(newline=""); w=csv.writer(buf,delimiter=";"); w.writerow(headers); w.writerows([list(r) for r in con.execute(sql)])
        data=("\ufeff"+buf.getvalue()).encode("utf-8")
        self.send_bytes(200,data,"text/csv; charset=utf-8",{"Content-Disposition":f'attachment; filename="mercado-{kind}-{today_iso()}.csv"'})


def main():
    init_db()
    print("="*64)
    print("MERCADO PRO | Gestão local de mercado")
    print(f"Acesse: http://{HOST}:{PORT}")
    print(f"Banco persistente: {DB_PATH}")
    print("Não exponha esta instância diretamente à internet.")
    print("Use Ctrl+C para encerrar; feche o navegador apenas quando quiser.")
    print("="*64)
    try:
        server=ThreadingHTTPServer((HOST,PORT),AppHandler)
        server.daemon_threads=True
        server.serve_forever(poll_interval=0.25)
    except OSError as e:
        print(f"Não foi possível iniciar o servidor em {HOST}:{PORT}: {e}",file=sys.stderr);sys.exit(1)
    except KeyboardInterrupt: print("\nServidor encerrado.")

if __name__=="__main__":main()
