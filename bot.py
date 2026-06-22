import os
import asyncio
import logging
from datetime import datetime, date, timedelta
import pytz
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FORTALEZA_TZ = pytz.timezone("America/Fortaleza")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL_PESSOAL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY_PESSOAL"]
SUPABASE_URL_GP = os.environ["SUPABASE_URL_GP"]
SUPABASE_KEY_GP = os.environ["SUPABASE_KEY_GP"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
sb_gp = create_client(SUPABASE_URL_GP, SUPABASE_KEY_GP)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

DIAS_PT = {0: "domingo", 1: "segunda", 2: "terça", 3: "quarta", 4: "quinta", 5: "sexta", 6: "sábado"}

SYSTEM_PROMPT = """Você é a secretaria pessoal do Giácomo Ponte, personal trainer de Fortaleza/CE.

Seu papel: organizar a rotina dele, lembrá-lo de compromissos, cobrar atividades planejadas e registrar eventos novos.

Tom: direto, sem enrolação, sem julgamento. Parceiro, não chefe. Máximo 3 linhas por mensagem.

Quando o usuário disser algo como "Juan marcou extra quinta 15h", extraia: nome, dia, hora e responda confirmando o registro.
Quando disser "treinei hoje" ou "não treinei", confirme e registre.
Quando disser "agenda hoje" ou "como tá a semana", liste os compromissos.

Sempre responda em português. Seja conciso."""


def agora():
    return datetime.now(FORTALEZA_TZ)


def hoje():
    return agora().date()


async def get_aulas_gp(data: date) -> list:
    dia_semana = data.weekday() + 1
    if dia_semana == 7:
        dia_semana = 0
    try:
        resp = sb_gp.table("agendas").select(
            "inicio, fim, days, students(nome)"
        ).eq("owner_uid", "ddd70b96-9a7c-4de4-add6-d5c9b4da382f").eq("ativo", True).eq("deleted", False).execute()

        aulas = []
        for a in (resp.data or []):
            days = a.get("days", [])
            if isinstance(days, list) and dia_semana in days:
                nome = a.get("students", {}).get("nome", "Aluno") if a.get("students") else "Aluno"
                aulas.append({"hora": a["inicio"], "nome": nome, "tipo": "aula"})
        return sorted(aulas, key=lambda x: x["hora"])
    except Exception as e:
        logger.error(f"Erro GP Manager: {e}")
        return []


async def get_rotina_dia(dia_semana: int) -> list:
    resp = sb.table("secretaria_rotina_semanal").select("*").eq("dia_semana", dia_semana).eq("ativo", True).execute()
    return resp.data or []


async def get_eventos_dia(data: date) -> list:
    resp = sb.table("secretaria_eventos").select("*").eq("data", str(data)).eq("cancelado", False).execute()
    return resp.data or []


async def registrar_evento(data: date, hora: str, descricao: str, tipo: str = "avulso") -> bool:
    try:
        sb.table("secretaria_eventos").insert({
            "data": str(data), "hora_inicio": hora, "descricao": descricao, "tipo": tipo
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Erro registrar evento: {e}")
        return False


async def registrar_confirmacao(data: date, tipo: str, realizado: bool, obs: str = None) -> bool:
    try:
        existing = sb.table("secretaria_confirmacoes").select("id").eq("data", str(data)).eq("tipo", tipo).execute()
        if existing.data:
            sb.table("secretaria_confirmacoes").update({
                "realizado": realizado, "observacao": obs
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.table("secretaria_confirmacoes").insert({
                "data": str(data), "tipo": tipo, "realizado": realizado, "observacao": obs
            }).execute()
        return True
    except Exception as e:
        logger.error(f"Erro confirmação: {e}")
        return False


async def montar_resumo_dia(data: date) -> str:
    dia_semana = data.weekday() + 1
    if dia_semana == 7:
        dia_semana = 0

    aulas = await get_aulas_gp(data)
    rotina = await get_rotina_dia(dia_semana)
    eventos = await get_eventos_dia(data)

    linhas = [f"📅 *{DIAS_PT[dia_semana].capitalize()}, {data.strftime('%d/%m')}*\n"]

    if aulas:
        linhas.append("*Aulas:*")
        for a in aulas:
            linhas.append(f"  {a['hora']} — {a['nome']}")

    if eventos:
        linhas.append("\n*Compromissos avulsos:*")
        for e in eventos:
            hora = e.get("hora_inicio", "") or ""
            linhas.append(f"  {hora} — {e['descricao']}")

    if rotina:
        linhas.append("\n*Rotina:*")
        for r in rotina:
            emoji = {"treino": "🏋", "projeto": "💻", "descanso": "😴"}.get(r["tipo"], "📌")
            linhas.append(f"  {emoji} {r['hora_inicio'][:5]} — {r['descricao']}")

    if not aulas and not eventos and not rotina:
        linhas.append("Dia livre. Aproveita para descansar.")

    return "\n".join(linhas)


async def processar_com_claude(texto: str, contexto_dia: str) -> str:
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Contexto da agenda hoje:\n{contexto_dia}\n\nMensagem do Giácomo: {texto}"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        logger.error(f"Erro Claude: {e}")
        return "Não consegui processar isso agora. Tenta de novo."


async def enviar_bom_dia(app: Application):
    data = hoje()
    resumo = await montar_resumo_dia(data)
    await app.bot.send_message(chat_id=CHAT_ID, text=f"Bom dia, Giácomo! ☀️\n\n{resumo}", parse_mode="Markdown")


async def enviar_check_tarde(app: Application):
    data = hoje()
    dia_semana = data.weekday() + 1
    if dia_semana == 7:
        dia_semana = 0

    rotina = await get_rotina_dia(dia_semana)
    treino_hoje = any(r["tipo"] == "treino" for r in rotina)
    projeto_hoje = any(r["tipo"] == "projeto" for r in rotina)

    partes = ["🔔 Check-in"]
    if treino_hoje:
        conf = sb.table("secretaria_confirmacoes").select("realizado").eq("data", str(data)).eq("tipo", "treino").execute()
        if not conf.data:
            partes.append("Treino tá na agenda hoje. Já foi?")
    if projeto_hoje:
        partes.append("Bloco de GP Manager reservado. Conseguiu trabalhar?")
    if len(partes) == 1:
        partes.append("Tudo certo por aí?")

    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(partes))


async def enviar_resumo_noite(app: Application):
    data = hoje()
    dia_semana = data.weekday() + 1
    if dia_semana == 7:
        dia_semana = 0

    rotina = await get_rotina_dia(dia_semana)
    confs = sb.table("secretaria_confirmacoes").select("*").eq("data", str(data)).execute()
    confs_dict = {c["tipo"]: c["realizado"] for c in (confs.data or [])}

    linhas = ["📊 *Resumo do dia*\n"]
    for r in rotina:
        realizado = confs_dict.get(r["tipo"])
        emoji = "✅" if realizado is True else "❌" if realizado is False else "⚪"
        linhas.append(f"{emoji} {r['descricao']}")

    if not rotina:
        linhas.append("Dia livre hoje.")

    linhas.append("\nComo foi o dia?")
    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(linhas), parse_mode="Markdown")


async def cobrar_treino_nao_confirmado(app: Application):
    data = hoje()
    dia_semana = data.weekday() + 1
    if dia_semana == 7:
        dia_semana = 0

    rotina = await get_rotina_dia(dia_semana)
    if not any(r["tipo"] == "treino" for r in rotina):
        return

    conf = sb.table("secretaria_confirmacoes").select("*").eq("data", str(data)).eq("tipo", "treino").execute()
    if not conf.data:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="🏋 Treino estava na agenda hoje e não recebi confirmação. Treinou? Responde sim ou não."
        )


async def agendar_lembretes_aulas(app: Application):
    job_queue = app.job_queue
    data = hoje()
    aulas = await get_aulas_gp(data)
    agora_ftz = agora()

    async def lembrete_job(context: ContextTypes.DEFAULT_TYPE):
        _, nome, hora = context.job.data
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⏰ Em 30 minutos: aula de *{nome}* às {hora}",
            parse_mode="Markdown"
        )

    for aula in aulas:
        h, m = map(int, aula["hora"].split(":"))
        aula_dt = agora_ftz.replace(hour=h, minute=m, second=0, microsecond=0)
        lembrete_dt = aula_dt - timedelta(minutes=30)
        if lembrete_dt > agora_ftz:
            delay = (lembrete_dt - agora_ftz).total_seconds()
            job_queue.run_once(lembrete_job, when=delay, data=(str(data), aula["nome"], aula["hora"]))


async def agendar_jobs(app: Application):
    jq = app.job_queue
    agora_ftz = agora()

    def segundos_ate(h, m):
        target = agora_ftz.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= agora_ftz:
            target += timedelta(days=1)
        return (target - agora_ftz).total_seconds()

    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_bom_dia(app)), interval=86400, first=segundos_ate(6, 0))
    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_check_tarde(app)), interval=86400, first=segundos_ate(12, 30))
    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_resumo_noite(app)), interval=86400, first=segundos_ate(19, 0))
    jq.run_repeating(lambda ctx: asyncio.create_task(cobrar_treino_nao_confirmado(app)), interval=86400, first=segundos_ate(20, 30))

    await agendar_lembretes_aulas(app)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    texto = update.message.text.lower().strip()
    data = hoje()

    if any(p in texto for p in ["treinei", "fiz o treino", "sim, treinei", "fiz treino"]):
        await registrar_confirmacao(data, "treino", True)
        await update.message.reply_text("✅ Treino registrado. Bom trabalho.")
        return

    if any(p in texto for p in ["não treinei", "nao treinei", "pulei", "não fiz", "nao fiz"]):
        await registrar_confirmacao(data, "treino", False)
        await update.message.reply_text("Registrado. Sem drama — o próximo tá aí.")
        return

    if any(p in texto for p in ["agenda hoje", "o que tem hoje", "como tá hoje", "minha agenda"]):
        await update.message.reply_text(await montar_resumo_dia(data), parse_mode="Markdown")
        return

    if any(p in texto for p in ["semana", "essa semana", "agenda semana"]):
        linhas = []
        for i in range(7):
            d = data + timedelta(days=i)
            linhas.append(await montar_resumo_dia(d))
            linhas.append("")
        await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")
        return

    contexto_dia = await montar_resumo_dia(data)
    resposta = await processar_com_claude(update.message.text, contexto_dia)
    await update.message.reply_text(resposta)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Secretaria ativa. 🗂️\n\n"
        "*Comandos rápidos:*\n"
        "• *agenda hoje* — ver o dia\n"
        "• *semana* — ver a semana\n"
        "• *treinei* — confirmar treino\n"
        "• *não treinei* — registrar falta\n"
        "• Fale naturalmente: _'Juan marcou extra quinta 15h'_\n\n"
        "Lembretes automáticos: 06h • 12h30 • 19h • 20h30",
        parse_mode="Markdown"
    )


async def cmd_treino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await registrar_confirmacao(hoje(), "treino", True)
    await update.message.reply_text("✅ Treino confirmado.")


async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = hoje()
    linhas = []
    for i in range(7):
        d = data + timedelta(days=i)
        linhas.append(await montar_resumo_dia(d))
        linhas.append("")
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("treino", cmd_treino))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.post_init = lambda a: asyncio.create_task(agendar_jobs(a))
    logger.info("Secretaria bot iniciando...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
