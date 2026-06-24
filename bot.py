import os
import asyncio
import logging
from datetime import datetime, date, timedelta
import pytz
from telegram import Update
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
SUPABASE_KEY_GP = os.environ.get("SUPABASE_SERVICE_KEY_GP") or os.environ["SUPABASE_KEY_GP"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
sb_gp = create_client(SUPABASE_URL_GP, SUPABASE_KEY_GP)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 0=Dom, 1=Seg, 2=Ter, 3=Qua, 4=Qui, 5=Sex, 6=Sab (igual ao banco)
DIAS_PT = {0: "domingo", 1: "segunda", 2: "terça", 3: "quarta", 4: "quinta", 5: "sexta", 6: "sábado"}

SYSTEM_PROMPT = """Você é a secretaria pessoal do Giácomo Ponte, personal trainer de Fortaleza/CE.

REGRAS RÍGIDAS:
1. Só registre, remarque ou cancele algo se o usuário pedir EXPLICITAMENTE. Nunca infira ações.
2. Se o usuário disser "fiz ajustes no banco", apenas confirme que vai reler a agenda. Não invente remarcações.
3. Quando registrar algo, confirme apenas o que foi pedido — sem adicionar informações extras.
4. Seja direto. Máximo 2 linhas. Sem elogios, sem emojis excessivos.
5. Se não entendeu o pedido, pergunte objetivamente. Nunca assuma.

Você tem memória da conversa recente. Use-a para manter contexto, não para inventar ações."""


def agora_ftz() -> datetime:
    return datetime.now(FORTALEZA_TZ)


def hoje_ftz() -> date:
    """Retorna a data atual no timezone de Fortaleza (UTC-3), independente do servidor."""
    return agora_ftz().date()


def dia_semana_db(d: date) -> int:
    """Converte date para o padrão do banco: 0=Dom, 1=Seg, ..., 6=Sab."""
    # Python weekday(): 0=Seg, 6=Dom
    # Banco: 0=Dom, 1=Seg, ..., 6=Sab
    return (d.weekday() + 1) % 7


async def get_aulas_gp(data: date) -> list:
    dia = dia_semana_db(data)
    try:
        resp = sb_gp.table("agendas").select(
            "inicio, fim, days, students(nome, status)"
        ).eq("owner_uid", "ddd70b96-9a7c-4de4-add6-d5c9b4da382f").eq("ativo", "true").eq("deleted", "false").execute()

        logger.info(f"GP Manager - registros raw: {len(resp.data or [])}")
        if resp.data:
            logger.info(f"GP Manager - sample: {resp.data[0]}")

        aulas = []
        for a in (resp.data or []):
            days = a.get("days", [])
            student = a.get("students") or {}
            status = student.get("status", "")
            if isinstance(days, list) and dia in days and status not in ("inativo", "aguardando"):
                nome = student.get("nome", "Aluno")
                aulas.append({"hora": a["inicio"], "nome": nome})
        return sorted(aulas, key=lambda x: x["hora"])
    except Exception as e:
        logger.error(f"Erro GP Manager: {e}")
        return []


async def get_rotina_dia(data: date) -> list:
    dia = dia_semana_db(data)
    resp = sb.table("secretaria_rotina_semanal").select("*").eq("dia_semana", dia).eq("ativo", True).execute()
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
    dia = dia_semana_db(data)
    aulas = await get_aulas_gp(data)
    rotina = await get_rotina_dia(data)
    eventos = await get_eventos_dia(data)

    linhas = [f"📅 *{DIAS_PT[dia].capitalize()}, {data.strftime('%d/%m')}*\n"]

    itens = []

    for a in aulas:
        itens.append({"hora": a["hora"], "texto": f"👤 {a['hora']} — {a['nome']}"})

    for r in rotina:
        emoji = {"treino": "🏋", "projeto": "💻", "descanso": "😴", "outro": "📌"}.get(r["tipo"], "📌")
        itens.append({"hora": r["hora_inicio"][:5], "texto": f"{emoji} {r['hora_inicio'][:5]} — {r['descricao']}"})

    for e in eventos:
        hora = e.get("hora_inicio") or "00:00"
        itens.append({"hora": hora[:5], "texto": f"🔹 {hora[:5]} — {e['descricao']}"})

    itens.sort(key=lambda x: x["hora"])

    if itens:
        linhas += [i["texto"] for i in itens]
    else:
        linhas.append("Dia livre. Descansa.")

    return "\n".join(linhas)


async def salvar_mensagem(role: str, content: str):
    sb.table("secretaria_historico_conversa").insert({
        "role": role, "content": content
    }).execute()


async def get_historico(limite: int = 10) -> list:
    resp = sb.table("secretaria_historico_conversa").select("role, content").order("created_at", desc=True).limit(limite).execute()
    msgs = resp.data or []
    msgs.reverse()
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


async def processar_com_claude(texto: str, contexto_dia: str) -> str:
    await salvar_mensagem("user", texto)
    historico = await get_historico(10)

    messages = historico if historico else [{"role": "user", "content": texto}]

    try:
        resp = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT + f"\n\nContexto da agenda hoje:\n{contexto_dia}",
            messages=messages
        )
        resposta = resp.content[0].text
        await salvar_mensagem("assistant", resposta)
        return resposta
    except Exception as e:
        logger.error(f"Erro Claude: {e}")
        return "Não consegui processar agora. Tenta de novo."


async def enviar_bom_dia(app: Application):
    data = hoje_ftz()
    resumo = await montar_resumo_dia(data)
    await app.bot.send_message(chat_id=CHAT_ID, text=f"Bom dia! ☀️\n\n{resumo}", parse_mode="Markdown")


async def enviar_check_tarde(app: Application):
    data = hoje_ftz()
    rotina = await get_rotina_dia(data)
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
        partes.append("Tudo certo?")

    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(partes))


async def enviar_resumo_noite(app: Application):
    data = hoje_ftz()
    rotina = await get_rotina_dia(data)
    confs = sb.table("secretaria_confirmacoes").select("*").eq("data", str(data)).execute()
    confs_dict = {c["tipo"]: c["realizado"] for c in (confs.data or [])}

    linhas = ["📊 *Resumo do dia*\n"]
    for r in rotina:
        realizado = confs_dict.get(r["tipo"])
        emoji = "✅" if realizado is True else "❌" if realizado is False else "⚪"
        linhas.append(f"{emoji} {r['descricao']}")

    if not rotina:
        linhas.append("Dia livre hoje.")

    linhas.append("\nComo foi?")
    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(linhas), parse_mode="Markdown")


async def cobrar_treino(app: Application):
    data = hoje_ftz()
    rotina = await get_rotina_dia(data)
    if not any(r["tipo"] == "treino" for r in rotina):
        return
    conf = sb.table("secretaria_confirmacoes").select("*").eq("data", str(data)).eq("tipo", "treino").execute()
    if not conf.data:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="🏋 Treino estava na agenda e não recebi confirmação. Treinou?"
        )


async def agendar_lembretes_aulas(app: Application):
    data = hoje_ftz()
    aulas = await get_aulas_gp(data)
    agora = agora_ftz()

    async def lembrete_job(context: ContextTypes.DEFAULT_TYPE):
        _, nome, hora = context.job.data
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⏰ Em 30 min: *{nome}* às {hora}",
            parse_mode="Markdown"
        )

    for aula in aulas:
        h, m = map(int, aula["hora"].split(":"))
        aula_dt = agora.replace(hour=h, minute=m, second=0, microsecond=0)
        lembrete_dt = aula_dt - timedelta(minutes=30)
        if lembrete_dt > agora:
            delay = (lembrete_dt - agora).total_seconds()
            app.job_queue.run_once(lembrete_job, when=delay, data=(str(data), aula["nome"], aula["hora"]))


def segundos_ate(h: int, m: int) -> float:
    agora = agora_ftz()
    target = agora.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= agora:
        target += timedelta(days=1)
    return (target - agora).total_seconds()


async def agendar_jobs(app: Application):
    jq = app.job_queue
    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_bom_dia(app)), interval=86400, first=segundos_ate(6, 0))
    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_check_tarde(app)), interval=86400, first=segundos_ate(12, 30))
    jq.run_repeating(lambda ctx: asyncio.create_task(enviar_resumo_noite(app)), interval=86400, first=segundos_ate(19, 0))
    jq.run_repeating(lambda ctx: asyncio.create_task(cobrar_treino(app)), interval=86400, first=segundos_ate(20, 30))
    await agendar_lembretes_aulas(app)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    texto = update.message.text.lower().strip()
    data = hoje_ftz()

    if any(p in texto for p in ["treinei", "fiz o treino", "fiz treino"]):
        await registrar_confirmacao(data, "treino", True)
        await update.message.reply_text("✅ Treino registrado.")
        return

    if any(p in texto for p in ["não treinei", "nao treinei", "pulei", "não fiz", "nao fiz"]):
        await registrar_confirmacao(data, "treino", False)
        await update.message.reply_text("Registrado. Próximo tá aí.")
        return

    if any(p in texto for p in ["agenda hoje", "o que tem hoje", "como tá hoje", "minha agenda"]):
        await update.message.reply_text(await montar_resumo_dia(data), parse_mode="Markdown")
        return

    if any(p in texto for p in ["semana", "essa semana", "agenda semana"]):
        msgs = []
        for i in range(7):
            d = data + timedelta(days=i)
            msgs.append(await montar_resumo_dia(d))
        await update.message.reply_text("\n\n".join(msgs), parse_mode="Markdown")
        return

    contexto_dia = await montar_resumo_dia(data)
    resposta = await processar_com_claude(update.message.text, contexto_dia)
    await update.message.reply_text(resposta)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Secretaria ativa. 🗂️\n\n"
        "*Comandos:*\n"
        "• *agenda hoje*\n"
        "• *semana*\n"
        "• *treinei* / *não treinei*\n"
        "• Linguagem natural: _'Juan marcou extra quinta 15h'_\n\n"
        "Lembretes: 06h • 12h30 • 19h • 20h30",
        parse_mode="Markdown"
    )


async def cmd_treino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await registrar_confirmacao(hoje_ftz(), "treino", True)
    await update.message.reply_text("✅ Treino confirmado.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("treino", cmd_treino))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.post_init = agendar_jobs
    logger.info("Secretaria bot v2 iniciando...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
