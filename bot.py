import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import db

logging.basicConfig(level=logging.INFO)
bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()
_giga = None
_claude_client = None


def fmt(n) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def system_prompt() -> str:
    products = "\n".join(
        f"- {p['title']} — {fmt(p['price'])} ₽. {p['desc']}" for p in config.PRODUCTS.values()
    )
    return config.SYSTEM_PROMPT.format(
        name=config.BUSINESS_NAME, products=products, knowledge=config.KNOWLEDGE
    )


def menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p['title']} · {fmt(p['price'])} ₽", callback_data=f"buy:{key}")]
        for key, p in config.PRODUCTS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _gigachat(history):
    global _giga
    if _giga is None:
        from gigachat import GigaChat

        _giga = GigaChat(
            credentials=config.GIGACHAT_CREDENTIALS,
            scope="GIGACHAT_API_PERS",
            model=config.GIGACHAT_MODEL,
            verify_ssl_certs=False,  # иначе нужен сертификат Минцифры
        )
    messages = [{"role": "system", "content": system_prompt()}] + history
    resp = await _giga.achat({"messages": messages})
    return resp.choices[0].message.content, 0.0  # бесплатный тариф: расход 0


_groq_client = None


async def _groq(history):
    global _groq_client
    if _groq_client is None:
        from openai import AsyncOpenAI

        _groq_client = AsyncOpenAI(
            api_key=config.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1"
        )
    messages = [{"role": "system", "content": system_prompt()}] + history
    resp = await _groq_client.chat.completions.create(
        model=config.GROQ_MODEL, messages=messages, max_tokens=500
    )
    return resp.choices[0].message.content, 0.0  # бесплатный тариф: расход 0


async def _claude(history):
    global _claude_client
    if _claude_client is None:
        from anthropic import AsyncAnthropic

        _claude_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = await _claude_client.messages.create(
        model=config.AI_MODEL, max_tokens=500, system=system_prompt(), messages=history
    )
    cost_usd = (
        resp.usage.input_tokens * config.PRICE_IN_USD + resp.usage.output_tokens * config.PRICE_OUT_USD
    ) / 1_000_000
    return resp.content[0].text, cost_usd * config.USD_RUB


async def ask_ai(user_id: int, text: str) -> str:
    db.add_message(user_id, "user", text)
    history = db.get_history(user_id, config.HISTORY_LIMIT)
    if config.AI_PROVIDER == "groq":
        answer, cost_rub = await _groq(history)
    elif config.AI_PROVIDER == "gigachat":
        answer, cost_rub = await _gigachat(history)
    else:
        answer, cost_rub = await _claude(history)
    db.add_message(user_id, "assistant", answer)
    db.add_ai_cost(user_id, cost_rub)
    return answer


# ---------- клиент ----------
@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(m: Message):
    await m.answer(
        f"Здравствуйте! Я ИИ-менеджер «{config.BUSINESS_NAME}». "
        "Задайте любой вопрос или выберите продукт:",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = config.PRODUCTS.get(key)
    if not p:
        return await cb.answer("Продукт не найден", show_alert=True)
    user = cb.from_user
    order_id = db.create_order(user.id, user.username or user.full_name, p["title"], p["price"])
    pay_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=config.PAYMENT_LINK)],
        [InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"sent:{order_id}")],
    ])
    await cb.message.answer(
        f"Заказ №{order_id}: {p['title']}, {fmt(p['price'])} ₽.\n{config.PAYMENT_NOTE}",
        reply_markup=pay_kb,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("sent:"))
async def sent(cb: CallbackQuery):
    order_id = int(cb.data.split(":")[1])
    user = cb.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить оплату", callback_data=f"paid:{order_id}")]
    ])
    for admin_id in config.ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"Клиент @{user.username or user.full_name} сообщает об оплате заказа №{order_id}. "
            "Проверьте поступление и подтвердите.",
            reply_markup=kb,
        )
    await cb.message.answer("Спасибо! Проверяем оплату, это займёт немного времени.")
    await cb.answer()


# ---------- админ ----------
@dp.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    order = db.mark_paid(int(cb.data.split(":")[1]))
    if not order:
        return await cb.answer("Уже подтверждён или не найден", show_alert=True)
    await bot.send_message(order["user_id"], f"Оплата получена. Спасибо! «{order['product']}» за вами.")
    await cb.message.edit_text(f"Заказ №{order['id']} подтверждён ✅")
    await cb.answer()


@dp.message(Command("stats"))
async def stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    by_product, cost, dialogs = db.stats(7)
    sales = sum(r["n"] for r in by_product)
    revenue = sum(r["s"] or 0 for r in by_product)
    avg = revenue / sales if sales else 0
    lines = [
        "📊 За 7 дней",
        f"Выручка: {fmt(revenue)} ₽",
        f"Продаж: {sales} · средний чек {fmt(avg)} ₽",
        f"Диалогов: {dialogs}",
    ]
    lines += [f"  • {r['product']}: {r['n']}" for r in by_product]
    lines += ["", "🤖 Окупаемость ИИ-менеджера"]
    if cost > 0 and revenue > 0:
        lines.append(f"×{revenue / cost:.1f} выручки на каждый рубль расходов")
    lines.append(f"Расход: {fmt(cost)} ₽")
    if sales:
        lines.append(f"На продажу: {fmt(cost / sales)} ₽")
    await m.answer("\n".join(lines))


# ---------- диалог с ИИ (должен быть последним) ----------
@dp.message(F.text)
async def talk(m: Message):
    await bot.send_chat_action(m.chat.id, "typing")
    try:
        answer = await ask_ai(m.from_user.id, m.text)
    except Exception:
        logging.exception("AI error")
        answer = "Извините, небольшой сбой. Напишите ещё раз через минуту."
    await m.answer(answer, reply_markup=menu_kb())


async def main():
    db.init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
