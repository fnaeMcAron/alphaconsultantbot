import logging
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .answer import answer_question
from .config import load_settings
from .analytics import Events
from .dialog import LastQuestion, PendingStore, looks_like_reply
from .profile import (LABELS, QUESTIONS, UNKNOWN, ProfileStore, extract_profile, is_statement_only, next_question,
                      parse_field)
from .recommend import is_recommend_request, recommend
from .kb.build import build
from .kb.search import KnowledgeBase
from .llm import LLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

WELCOME = (
    "Здравствуйте! Я помощник «Альфа-Курса»: отвечаю на вопросы предпринимателей "
    "и подсказываю полезные материалы.\n\nЗадайте вопрос своими словами, например: "
    "«Как платить налоги на УСН?» или «Как продвигать товары на Ozon?»\n\n"
    "Если расскажете о своём бизнесе (например, «у меня ИП на патенте»), я запомню это и буду отвечать точнее. "
    "Посмотреть сохранённое: /profile, удалить: /forget. Подсказки о материалах после ответов можно отключить: /recs."
)


def ensure_kb(cfg) -> bool:
    if cfg.kb_path.exists():
        return True
    if not cfg.dump_path.exists():
        log.warning("Нет ни %s, ни %s: положите дамп в %s (см. README).", cfg.kb_path, cfg.dump_path, cfg.data_dir)
        return False
    log.info("Строю индекс базы знаний из %s ...", cfg.dump_path)
    stats = build(cfg.dump_path, cfg.kb_path)
    log.info("Готово: %s фрагментов", stats["_chunks"])
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["last"].clear(update.effective_chat.id)
    await update.message.reply_text(WELCOME)


def _keyboard(prefix: str, labels: list, per_row: int = 1) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(lab[:40], callback_data=f"{prefix}:{i}") for i, lab in enumerate(labels)]
    return InlineKeyboardMarkup([btns[i:i + per_row] for i in range(0, len(btns), per_row)])


async def respond(context: ContextTypes.DEFAULT_TYPE, message, text: str, prior: str = "", prefix: str = "",
                  ask_profile: bool = True, use_prev: bool = True) -> None:
    cfg, kb, llm, pending, profiles, last = (
        context.bot_data.get(k) for k in ("cfg", "kb", "llm", "pending", "profiles", "last"))
    if kb is None:
        await message.reply_text("База знаний пока не загружена. Сообщите администратору бота.")
        return
    chat_id = message.chat_id
    prof = profiles.get(chat_id)
    events = context.bot_data.get("events")
    await message.chat.send_action("typing")
    combined = f"{prior} {text}".strip()
    if is_recommend_request(combined):
        block = recommend(kb, cfg, prof, combined, explicit=True)
        if block.needs_topic:
            pending.put(chat_id, combined, [])
            await message.reply_text("По какой теме подобрать материалы? Например: налоги, маркетинг, запуск бизнеса, продажи.")
            return
        profiles.save(chat_id, prof)
        if events:
            events.log(chat_id, "rec_request", shown=block.items, paid=block.paid)
        await message.reply_text(
            (prefix + "Вот что подобрал по вашему запросу:\n\n" + block.text) if block.text else
            prefix + "Подходящих материалов по этому запросу в каталоге не нашёл. Попробуйте назвать тему иначе.",
            disable_web_page_preview=True)
        return
    try:
        prev = last.get(chat_id) if use_prev and not prior else ""  # для коротких реплик-продолжений
        reply = await answer_question(cfg, kb, llm, text, context=prior, prev=prev, profile=prof)
    except Exception:
        log.exception("Ошибка при ответе")
        await message.reply_text("Что-то пошло не так. Попробуйте задать вопрос ещё раз.")
        return
    out, markup = prefix + reply.text[:3200], None
    if reply.status == "clarify" and reply.expects_reply:
        pending.put(chat_id, prior or text, reply.options)
        if reply.options:
            markup = _keyboard("opt", reply.options)
    elif reply.status == "answered":
        if reply.standalone:
            last.put(chat_id, reply.standalone)
        prof.msgs += 1
        name = next_question(prof, f"{prior} {text}") if ask_profile else None
        if name:
            prof.asked.append(name)
            prof.last_asked = prof.msgs
            question, options = QUESTIONS[name]
            out += "\n\n" + question
            markup = _keyboard(f"prof:{name}", [lab for lab, _ in options], per_row=2)
            pending.put(chat_id, f"{prior} {text}".strip(), [], kind="profile", field=name)
        elif prof.recs:
            block = recommend(kb, cfg, prof, reply.standalone or text, exclude={x.item_id for x in reply.sources})
            if block and block.text:
                out += "\n\n" + block.text
                if events:
                    events.log(chat_id, "rec_shown", shown=block.items, paid=block.paid)
        profiles.save(chat_id, prof)
    if events:
        events.log(chat_id, "answer", status=reply.status)
    await message.reply_text(out[:4000], disable_web_page_preview=True, reply_markup=markup)


async def apply_profile_answer(context, message, field_name: str, value: str, question: str) -> None:
    profiles = context.bot_data["profiles"]
    prof = profiles.get(message.chat_id)
    prof.set(field_name, value)
    profiles.save(message.chat_id, prof)
    if value == UNKNOWN:
        await message.reply_text("Хорошо, отвечаю без этого уточнения. Если захотите, расскажите о бизнесе в любой момент.")
        return
    label = LABELS.get(field_name, {}).get(value, value)
    prefix = f"Записал: {label}. Тогда отвечу точнее.\n\n" if question else f"Записал: {label}.\n\n"
    if question:
        await respond(context, message, question, prefix=prefix, ask_profile=False, use_prev=False)
    else:
        await message.reply_text(prefix.strip())


async def on_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, message = update.message.text.strip(), update.message
    profiles, pending = context.bot_data["profiles"], context.bot_data["pending"]
    pend = pending.pop(message.chat_id)
    if pend and pend.kind == "profile":
        value = parse_field(pend.field, text)
        if value is not None:
            await apply_profile_answer(context, message, pend.field, value, pend.question)
            return
        pend = None
    prior = pend.question if pend and looks_like_reply(text) else ""
    if not prior:
        found = extract_profile(text)
        if found:
            prof = profiles.get(message.chat_id)
            for name, value in found.items():
                prof.set(name, value)
            profiles.save(message.chat_id, prof)
            if is_statement_only(text):
                await message.reply_text(f"Запомнил: {prof.summary()}.\nЗадайте вопрос по бизнесу, и я отвечу с учётом этого. "
                                         "Посмотреть или удалить данные: /profile, /forget.")
                return
    await respond(context, message, text, prior)


async def on_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pend = context.bot_data["pending"].pop(query.message.chat_id)
    idx = int(query.data.split(":")[1])
    if pend is None or pend.kind != "clarify" or idx >= len(pend.options):
        await query.message.reply_text("Этот вопрос уже устарел. Задайте его, пожалуйста, ещё раз.")
        return
    await query.edit_message_reply_markup(reply_markup=None)
    await respond(context, query.message, pend.options[idx], pend.question)


async def on_profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, name, idx = query.data.split(":")
    options = QUESTIONS.get(name, ("", []))[1]
    if int(idx) >= len(options):
        return
    pend = context.bot_data["pending"].pop(query.message.chat_id)
    question = pend.question if pend and pend.kind == "profile" and pend.field == name else ""
    await query.edit_message_reply_markup(reply_markup=None)
    await apply_profile_answer(context, query.message, name, options[int(idx)][1], question)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = context.bot_data["profiles"].get(update.message.chat_id).summary()
    await update.message.reply_text(
        f"Что я о вас знаю: {summary}.\nУдалить: /forget. Изменить: просто напишите, например, «у меня теперь патент»."
        if summary else "Пока я ничего не знаю о вашем бизнесе. Расскажите, например: «у меня ИП на УСН, продаю на Ozon».")


async def toggle_recs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profiles = context.bot_data["profiles"]
    prof = profiles.get(update.message.chat_id)
    prof.recs = not prof.recs
    profiles.save(update.message.chat_id, prof)
    await update.message.reply_text(
        "Хорошо, подсказки о материалах после ответов включены." if prof.recs else
        "Хорошо, не буду добавлять подсказки о материалах к ответам. Если захотите подборку, напишите, например: "
        "«посоветуй курс по маркетингу». Включить обратно: /recs.")


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["profiles"].delete(update.message.chat_id)
    context.bot_data["pending"].pop(update.message.chat_id)
    context.bot_data["last"].clear(update.message.chat_id)
    await update.message.reply_text("Готово, все сохранённые сведения о вашем бизнесе удалены.")


async def on_init(app: Application) -> None:
    me = await app.bot.get_me()
    log.info("Подключился к Telegram как @%s", me.username)


async def on_shutdown(app: Application) -> None:
    await app.bot_data["llm"].aclose()


def main() -> None:
    cfg = load_settings()
    if not cfg.telegram_token:
        sys.exit("Не задан TELEGRAM_BOT_TOKEN (см. env.example).")
    ready = ensure_kb(cfg)
    builder = (Application.builder().token(cfg.telegram_token).post_init(on_init).post_shutdown(on_shutdown)
               .concurrent_updates(True)
               .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
               .get_updates_connect_timeout(30).get_updates_read_timeout(30))
    if cfg.telegram_proxy:  # например http://host:port или socks5://host:port
        builder = builder.proxy(cfg.telegram_proxy).get_updates_proxy(cfg.telegram_proxy)
    app = builder.build()
    app.bot_data.update(cfg=cfg, llm=LLM(cfg), pending=PendingStore(), profiles=ProfileStore(cfg.state_path),
                         events=Events(cfg.state_path, cfg.analytics_salt), last=LastQuestion(),
                        kb=KnowledgeBase(cfg.kb_path, cfg.min_coverage, cfg.min_dense) if ready else None)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("forget", forget))
    app.add_handler(CommandHandler("recs", toggle_recs))
    app.add_handler(CallbackQueryHandler(on_option, pattern=r"^opt:\d+$"))
    app.add_handler(CallbackQueryHandler(on_profile_button, pattern=r"^prof:\w+:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_question))
    log.info("Запускаю бота, проверяю связь с Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
