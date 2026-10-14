"""
Админ-панель мониторинга позиций на витринах МФО
Запуск: streamlit run app.py
"""

import asyncio
import re
from datetime import datetime
from io import BytesIO

import httpx
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# ============== КОНФИГУРАЦИЯ ВИТРИН ==============
SOURCES = [
    {
        "id": "example",
        "name": "Example.ru (шаблон)",
        "url": "https://example.ru/loans",
        "card_selector": ".loan-card",
        "fields": {
            "lender": ".loan-card__lender",
            "amount": ".loan-card__amount",
            "term": ".loan-card__term",
            "rate": ".loan-card__rate",
            "psk": ".loan-card__psk",
            "link": "a",
        },
    },
    # Добавляйте реальные витрины сюда:
    # {
    #     "id": "banki",
    #     "name": "Banki.ru",
    #     "url": "https://www.banki.ru/microloans/",
    #     "card_selector": "div[data-test='microloan-card']",
    #     "fields": {
    #         "lender": "[data-test='company-name']",
    #         "amount": "[data-test='amount']",
    #         "term": "[data-test='term']",
    #         "rate": "[data-test='rate']",
    #         "psk": "[data-test='psk']",
    #         "link": "a",
    #     },
    # },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()


def extract_range(text: str | None) -> tuple[float | None, float | None]:
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
    if len(nums) >= 2:
        return nums[0], nums[1]
    return nums[0], nums[0]


def extract_text(card, selector: str) -> str | None:
    if not selector:
        return None
    node = card.select_one(selector)
    return clean_text(node.get_text(" ", strip=True)) if node else None


def extract_href(card, selector: str, base_url: str) -> str | None:
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


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text


def parse_offers(html: str, source: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(source["card_selector"])
    if not cards:
        raise ValueError(f"Карточки не найдены: {source['card_selector']}")

    offers = []
    for position, card in enumerate(cards, start=1):
        fields = source["fields"]
        amount_text = extract_text(card, fields.get("amount", ""))
        term_text = extract_text(card, fields.get("term", ""))
        rate_text = extract_text(card, fields.get("rate", ""))
        psk_text = extract_text(card, fields.get("psk", ""))

        min_amount, max_amount = extract_range(amount_text)
        min_term, max_term = extract_range(term_text)

        offers.append({
            "Витрина": source["name"],
            "Позиция": position,
            "Кредитор": extract_text(card, fields.get("lender", "")),
            "Сумма от": min_amount,
            "Сумма до": max_amount,
            "Срок от (дн)": min_term,
            "Срок до (дн)": max_term,
            "Ставка %/день": extract_range(rate_text)[0],
            "ПСК %/год": extract_range(psk_text)[0],
            "Сумма (сырое)": amount_text,
            "Срок (сырое)": term_text,
            "Ставка (сырое)": rate_text,
            "Ссылка": extract_href(card, fields.get("link", ""), source["url"]),
            "Время": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_id": source["id"],
        })
    return offers


async def parse_sources(selected_ids: list[str]) -> tuple[list[dict], list[str]]:
    selected = [s for s in SOURCES if s["id"] in selected_ids]
    all_offers = []
    errors = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for source in selected:
            try:
                html = await fetch_html(client, source["url"])
                offers = parse_offers(html, source)
                all_offers.extend(offers)
            except Exception as e:
                errors.append(f"{source['name']}: {e}")
            await asyncio.sleep(1.5)
    return all_offers, errors


def to_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Позиции")
    return output.getvalue()


# ==================== UI ====================
st.set_page_config(
    page_title="Мониторинг витрин МФО",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==================== AUTH (опционально) ====================
def check_password():
    """Простая защита паролем. Пароль берётся из переменной окружения APP_PASSWORD."""
    import os
    password = os.getenv("APP_PASSWORD")
    if not password:
        return True  # пароль не задан — доступ открыт

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    st.title("🔐 Доступ к мониторингу")
    pwd = st.text_input("Пароль", type="password")
    if st.button("Войти"):
        if pwd == password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Неверный пароль")
    return False


if not check_password():
    st.stop()

st.markdown("""
<style>
    .main-header {font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem;}
    .sub-header {color: #6b7280; margin-bottom: 1.5rem;}
    div[data-testid="stMetricValue"] {font-size: 1.8rem;}
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="main-header">📊 Мониторинг позиций на витринах</p>', unsafe_allow_html=True)
st.markdown('<p class="sub-header">Выберите витрины, запустите парсинг и смотрите позиции конкурентов</p>', unsafe_allow_html=True)

# Sidebar
with st.sidebar:
    st.header("Настройки")
    source_names = {s["id"]: s["name"] for s in SOURCES}
    selected_ids = st.multiselect(
        "Витрины для парсинга",
        options=list(source_names.keys()),
        format_func=lambda x: source_names[x],
        default=list(source_names.keys())[:1] if SOURCES else [],
    )

    lender_filter = st.text_input("Фильтр по кредитору (необязательно)", placeholder="например: Займер")

    st.divider()
    parse_btn = st.button("🚀 Запустить парсинг", type="primary", use_container_width=True)

# Main area
if "results" not in st.session_state:
    st.session_state.results = None
    st.session_state.errors = []

if parse_btn:
    if not selected_ids:
        st.warning("Выберите хотя бы одну витрину")
    else:
        with st.spinner("Парсим витрины..."):
            offers, errors = asyncio.run(parse_sources(selected_ids))
            st.session_state.results = offers
            st.session_state.errors = errors

if st.session_state.errors:
    for err in st.session_state.errors:
        st.error(err)

if st.session_state.results:
    df = pd.DataFrame(st.session_state.results)

    # Фильтр по кредитору
    if lender_filter:
        df = df[df["Кредитор"].str.contains(lender_filter, case=False, na=False)]

    # Метрики
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Офферов найдено", len(df))
    col2.metric("Витрин", df["Витрина"].nunique() if not df.empty else 0)
    col3.metric("Уникальных кредиторов", df["Кредитор"].nunique() if not df.empty else 0)
    if not df.empty and "Позиция" in df.columns:
        best = df["Позиция"].min()
        col4.metric("Лучшая позиция", int(best) if pd.notna(best) else "—")

    st.subheader("Результаты")

    # Подсветка позиций
    def highlight_position(row):
        if row["Позиция"] <= 3:
            return ["background-color: #dcfce7"] * len(row)
        elif row["Позиция"] <= 10:
            return ["background-color: #fef9c3"] * len(row)
        return [""] * len(row)

    display_cols = [
        "Витрина", "Позиция", "Кредитор",
        "Сумма от", "Сумма до", "Срок от (дн)", "Срок до (дн)",
        "Ставка %/день", "ПСК %/год", "Ссылка", "Время"
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    styled = df[display_cols].style.apply(highlight_position, axis=1)
    st.dataframe(styled, use_container_width=True, height=500)

    # Экспорт
    st.subheader("Экспорт")
    col_csv, col_xlsx = st.columns(2)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with col_csv:
        csv_data = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Скачать CSV",
            csv_data,
            file_name=f"positions_{timestamp}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with col_xlsx:
        excel_data = to_excel(df)
        st.download_button(
            "📥 Скачать Excel",
            excel_data,
            file_name=f"positions_{timestamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

else:
    st.info("Выберите витрины слева и нажмите «Запустить парсинг»")

# Footer info
with st.expander("Как добавить новую витрину"):
    st.code("""
{
    "id": "banki",
    "name": "Banki.ru",
    "url": "https://www.banki.ru/microloans/",
    "card_selector": "div.card-class",   # селектор карточки оффера
    "fields": {
        "lender": ".lender-name",
        "amount": ".amount",
        "term": ".term",
        "rate": ".rate",
        "psk": ".psk",
        "link": "a",
    },
},
""", language="python")
    st.caption("После изменения списка SOURCES перезапустите приложение.")
