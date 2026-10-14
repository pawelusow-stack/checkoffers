"""
Админ-панель мониторинга позиций на витринах МФО
С авторизацией, историей позиций, графиками и массовым добавлением витрин.
"""

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from bs4 import BeautifulSoup
from yaml.loader import SafeLoader

# ==================== КОНФИГ ====================
DB_PATH = Path("monitor.db")
USERS_PATH = Path("users.yaml")
VITRINES_PATH = Path("vitrines.json")

DEFAULT_LENDERS = ["Кредиска", "Бериберу", "Хурма", "Займиго", "Давака", "Кредиска", "Beriberu", "Hurma", "Zaimigo", "Davaka"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# ==================== БАЗА И ФАЙЛЫ ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parsed_at TEXT,
            vitrine_id TEXT,
            vitrine_name TEXT,
            position INTEGER,
            lender TEXT,
            amount_from REAL,
            amount_to REAL,
            term_from REAL,
            term_to REAL,
            rate REAL,
            psk REAL,
            link TEXT
        )
    """)
    conn.commit()
    conn.close()

def load_vitrines():
    if VITRINES_PATH.exists():
        return json.loads(VITRINES_PATH.read_text(encoding="utf-8"))
    return [
        {
            "id": "example",
            "name": "Example (шаблон)",
            "url": "https://example.ru/loans",
            "card_selector": ".loan-card",
            "fields": {
                "lender": ".lender",
                "amount": ".amount",
                "term": ".term",
                "rate": ".rate",
                "psk": ".psk",
                "link": "a",
            },
        }
    ]

def save_vitrines(vitrines):
    VITRINES_PATH.write_text(json.dumps(vitrines, ensure_ascii=False, indent=2), encoding="utf-8")

def init_users():
    if not USERS_PATH.exists():
        # Начальный админ: admin / admin123
        hashed = stauth.Hasher(["admin123"]).generate()
        config = {
            "credentials": {
                "usernames": {
                    "admin": {
                        "email": "admin@example.com",
                        "name": "Администратор",
                        "password": hashed[0],
                        "role": "admin",
                    }
                }
            },
            "cookie": {
                "expiry_days": 30,
                "key": "mfo_monitor_secret_key_change_me",
                "name": "mfo_monitor_cookie",
            },
        }
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True)

def load_users():
    with open(USERS_PATH, encoding="utf-8") as f:
        return yaml.load(f, Loader=SafeLoader)

def save_users(config):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True)

# ==================== ПАРСИНГ ====================
def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()

def extract_range(text):
    if not text:
        return None, None
    text = text.replace("\xa0", " ").lower()
    numbers = re.findall(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    nums = [float(n.replace(",", ".")) for n in numbers]
    if not nums:
        return None, None
    has_ot = "от" in text
    has_do = "до" in text
    if has_ot and has_do and len(nums) >= 2:
        return nums[0], nums[1]
    if has_do and len(nums) == 1:
        return None, nums[0]
    if has_ot and len(nums) == 1:
        return nums[0], None
    return (nums[0], nums[1] if len(nums) >= 2 else nums[0])

def extract_text(card, selector):
    if not selector:
        return None
    node = card.select_one(selector)
    return clean_text(node.get_text(" ", strip=True)) if node else None

def extract_href(card, selector, base_url):
    if not selector:
        return None
    node = card.select_one(selector)
    if node and node.get("href"):
        href = node["href"]
        if href.startswith("http"):
            return href
        try:
            return str(httpx.URL(base_url).join(href))
        except Exception:
            return href
    return None

async def fetch_html(client, url):
    r = await client.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text

def parse_offers(html, source):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(source.get("card_selector", ".card"))
    offers = []
    for pos, card in enumerate(cards, 1):
        fields = source.get("fields", {})
        amount_text = extract_text(card, fields.get("amount"))
        term_text = extract_text(card, fields.get("term"))
        rate_text = extract_text(card, fields.get("rate"))
        psk_text = extract_text(card, fields.get("psk"))
        min_a, max_a = extract_range(amount_text)
        min_t, max_t = extract_range(term_text)
        offers.append({
            "vitrine_id": source["id"],
            "vitrine_name": source["name"],
            "position": pos,
            "lender": extract_text(card, fields.get("lender")),
            "amount_from": min_a,
            "amount_to": max_a,
            "term_from": min_t,
            "term_to": max_t,
            "rate": extract_range(rate_text)[0],
            "psk": extract_range(psk_text)[0],
            "link": extract_href(card, fields.get("link"), source["url"]),
        })
    return offers

def save_positions(offers):
    if not offers:
        return
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    for o in offers:
        conn.execute("""
            INSERT INTO positions (parsed_at, vitrine_id, vitrine_name, position, lender,
                                   amount_from, amount_to, term_from, term_to, rate, psk, link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, o["vitrine_id"], o["vitrine_name"], o["position"], o["lender"],
              o["amount_from"], o["amount_to"], o["term_from"], o["term_to"],
              o["rate"], o["psk"], o["link"]))
    conn.commit()
    conn.close()

async def run_parse(selected_ids):
    vitrines = load_vitrines()
    selected = [v for v in vitrines if v["id"] in selected_ids]
    all_offers = []
    errors = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for v in selected:
            try:
                html = await fetch_html(client, v["url"])
                offers = parse_offers(html, v)
                all_offers.extend(offers)
            except Exception as e:
                errors.append(f"{v['name']}: {e}")
            await asyncio.sleep(1.5)
    if all_offers:
        save_positions(all_offers)
    return all_offers, errors

# ==================== UI ====================
st.set_page_config(page_title="Мониторинг витрин МФО", page_icon="📊", layout="wide")

init_db()
init_users()
config = load_users()

authenticator = stauth.Authenticate(
    config["credentials"],
    config["cookie"]["name"],
    config["cookie"]["key"],
    config["cookie"]["expiry_days"],
)

name, authentication_status, username = authenticator.login("Вход", "main")

if authentication_status is False:
    st.error("Неверный логин или пароль")
elif authentication_status is None:
    st.warning("Введите логин и пароль")
elif authentication_status:
    authenticator.logout("Выйти", "sidebar")
    st.sidebar.success(f"Привет, {name}!")

    role = config["credentials"]["usernames"][username].get("role", "user")

    if role == "admin":
        tab1, tab2, tab3, tab4 = st.tabs(["📊 Мониторинг", "📈 История и графики", "🏢 Витрины", "👥 Пользователи"])
    else:
        tab1, tab2, tab3 = st.tabs(["📊 Мониторинг", "📈 История и графики", "🏢 Витрины"])
        tab4 = None

    # ===== TAB 1: Мониторинг =====
    with tab1:
        st.header("Текущие позиции")
        vitrines = load_vitrines()
        vitrine_map = {v["id"]: v["name"] for v in vitrines}
        selected = st.multiselect("Витрины", list(vitrine_map.keys()), format_func=lambda x: vitrine_map[x])

        default_filter = st.multiselect(
            "Фильтр по кредиторам (по умолчанию ваши)",
            DEFAULT_LENDERS,
            default=["Кредиска", "Бериберу", "Хурма", "Займиго", "Давака"],
        )
        custom_filter = st.text_input("Дополнительный фильтр по названию")

        if st.button("🚀 Запустить парсинг", type="primary"):
            if not selected:
                st.warning("Выберите витрины")
            else:
                with st.spinner("Парсим..."):
                    offers, errors = asyncio.run(run_parse(selected))
                    st.session_state["last_offers"] = offers
                    for e in errors:
                        st.error(e)

        if "last_offers" in st.session_state and st.session_state["last_offers"]:
            df = pd.DataFrame(st.session_state["last_offers"])
            # Фильтр
            mask = pd.Series([True] * len(df))
            if default_filter:
                pattern = "|".join(re.escape(x) for x in default_filter)
                mask &= df["lender"].str.contains(pattern, case=False, na=False)
            if custom_filter:
                mask &= df["lender"].str.contains(custom_filter, case=False, na=False)
            df = df[mask]

            st.dataframe(df, use_container_width=True)

            # Экспорт
            col1, col2 = st.columns(2)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            with col1:
                st.download_button("CSV", df.to_csv(index=False).encode("utf-8-sig"), f"positions_{ts}.csv")
            with col2:
                buf = BytesIO()
                df.to_excel(buf, index=False, engine="openpyxl")
                st.download_button("Excel", buf.getvalue(), f"positions_{ts}.xlsx")

    # ===== TAB 2: История =====
    with tab2:
        st.header("История позиций")
        period = st.selectbox("Период", ["Час", "День", "Неделя", "Месяц", "Всё время"])
        now = datetime.now()
        if period == "Час":
            since = now - timedelta(hours=1)
        elif period == "День":
            since = now - timedelta(days=1)
        elif period == "Неделя":
            since = now - timedelta(weeks=1)
        elif period == "Месяц":
            since = now - timedelta(days=30)
        else:
            since = datetime(2000, 1, 1)

        conn = sqlite3.connect(DB_PATH)
        hist = pd.read_sql_query(
            "SELECT * FROM positions WHERE parsed_at >= ?",
            conn, params=(since.isoformat(),)
        )
        conn.close()

        if not hist.empty:
            hist["parsed_at"] = pd.to_datetime(hist["parsed_at"])
            st.subheader("Таблица")
            st.dataframe(hist, use_container_width=True)

            st.subheader("График позиций")
            # Фильтр для графика
            lenders_in_hist = hist["lender"].dropna().unique().tolist()
            chosen_lenders = st.multiselect("Кредиторы на графике", lenders_in_hist, default=lenders_in_hist[:5] if lenders_in_hist else [])
            if chosen_lenders:
                plot_df = hist[hist["lender"].isin(chosen_lenders)]
                fig = px.line(
                    plot_df, x="parsed_at", y="position", color="lender",
                    facet_col="vitrine_name", markers=True,
                    title="Динамика позиций",
                    labels={"position": "Позиция", "parsed_at": "Время"}
                )
                fig.update_yaxes(autorange="reversed")  # 1 сверху
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Пока нет данных за выбранный период. Запустите парсинг.")

    # ===== TAB 3: Витрины =====
    with tab3:
        st.header("Управление витринами")
        vitrines = load_vitrines()
        st.dataframe(pd.DataFrame(vitrines)[["id", "name", "url"]], use_container_width=True)

        st.subheader("Массовое добавление")
        method = st.radio("Способ", ["Список URL", "Загрузка CSV/Excel"])

        if method == "Список URL":
            urls_text = st.text_area("Вставьте URL витрин (по одному на строку)", height=150)
            if st.button("Добавить из списка"):
                urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
                existing_ids = {v["id"] for v in vitrines}
                added = 0
                for url in urls:
                    domain = url.split("//")[-1].split("/")[0]
                    vid = domain.replace(".", "_")
                    if vid not in existing_ids:
                        vitrines.append({
                            "id": vid,
                            "name": domain,
                            "url": url,
                            "card_selector": ".card, .offer, .loan-item, div[class*='card']",
                            "fields": {
                                "lender": ".lender, .company, .name, h3, h4",
                                "amount": ".amount, .sum",
                                "term": ".term, .period",
                                "rate": ".rate, .percent",
                                "psk": ".psk",
                                "link": "a",
                            },
                        })
                        added += 1
                save_vitrines(vitrines)
                st.success(f"Добавлено {added} витрин (селекторы шаблонные — уточните вручную)")
                st.rerun()

        else:
            uploaded = st.file_uploader("CSV или Excel", type=["csv", "xlsx"])
            if uploaded and st.button("Загрузить"):
                if uploaded.name.endswith(".csv"):
                    df_up = pd.read_csv(uploaded)
                else:
                    df_up = pd.read_excel(uploaded)
                st.write("Предпросмотр:", df_up.head())
                # Ожидаем колонки: url, name (опционально)
                if "url" in df_up.columns:
                    existing_ids = {v["id"] for v in vitrines}
                    added = 0
                    for _, row in df_up.iterrows():
                        url = str(row["url"]).strip()
                        name = str(row.get("name", url.split("//")[-1].split("/")[0]))
                        vid = name.replace(" ", "_").lower()
                        if vid not in existing_ids:
                            vitrines.append({
                                "id": vid,
                                "name": name,
                                "url": url,
                                "card_selector": ".card, .offer",
                                "fields": {"lender": ".lender", "amount": ".amount", "term": ".term", "rate": ".rate", "psk": ".psk", "link": "a"},
                            })
                            added += 1
                    save_vitrines(vitrines)
                    st.success(f"Добавлено {added}")
                    st.rerun()

        # Удаление
        to_del = st.multiselect("Удалить витрины", [v["id"] for v in vitrines])
        if st.button("Удалить выбранные") and to_del:
            vitrines = [v for v in vitrines if v["id"] not in to_del]
            save_vitrines(vitrines)
            st.success("Удалено")
            st.rerun()

    # ===== TAB 4: Пользователи (только админ) =====
    if role == "admin" and tab4 is not None:
        with tab4:
            st.header("Управление пользователями")
            users_df = pd.DataFrame([
                {"Логин": u, "Имя": d["name"], "Роль": d.get("role", "user")}
                for u, d in config["credentials"]["usernames"].items()
            ])
            st.dataframe(users_df, use_container_width=True)

            st.subheader("Добавить пользователя")
            with st.form("add_user"):
                new_user = st.text_input("Логин")
                new_name = st.text_input("Имя")
                new_pass = st.text_input("Пароль", type="password")
                new_role = st.selectbox("Роль", ["user", "admin"])
                if st.form_submit_button("Добавить"):
                    if new_user and new_pass:
                        hashed = stauth.Hasher([new_pass]).generate()[0]
                        config["credentials"]["usernames"][new_user] = {
                            "email": f"{new_user}@example.com",
                            "name": new_name or new_user,
                            "password": hashed,
                            "role": new_role,
                        }
                        save_users(config)
                        st.success("Пользователь добавлен. Перезапустите приложение или перелогиньтесь.")
                        st.rerun()

            st.subheader("Удалить пользователя")
            del_user = st.selectbox("Выберите", [u for u in config["credentials"]["usernames"] if u != "admin"])
            if st.button("Удалить") and del_user:
                del config["credentials"]["usernames"][del_user]
                save_users(config)
                st.success("Удалено")
                st.rerun()

st.caption("История и пользователи хранятся в файлах на сервере. На Streamlit Cloud файлы могут сбрасываться при перезапуске контейнера.")
