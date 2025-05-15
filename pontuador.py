import os
import asyncpg
import logging
import sys
import asyncio
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# --- Configuração e constantes ---
BOT_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not BOT_TOKEN:
    logger.error("TELEGRAM_TOKEN não encontrado.")
    sys.exit(1)

DATABASE_URL = os.getenv('DATABASE_URL')
ID_ADMIN = 123456789
LIMIAR_PONTUADOR = 500
NIVEIS_BRINDES = {1000: 'Brinde Nível 1', 2000: 'Brinde Nível 2'}
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(',') if x.strip()]
    except ValueError:
        logger.error("ADMIN_IDS deve conter apenas números separados por vírgula.")
        ADMIN_IDS = []
else:
    ADMIN_IDS = []


# --- Estados de conversa ---

ESPERANDO_SUPORTE, AGUARDANDO_SENHA = range(2)


# --- Banco de dados ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            pontos INTEGER NOT NULL DEFAULT 0,
            nivel_atingido INTEGER NOT NULL DEFAULT 0,
            is_pontuador BOOLEAN NOT NULL DEFAULT FALSE,
            visto BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS historico_pontos (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES usuarios(user_id),
            pontos INTEGER NOT NULL,
            motivo TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS usuarios_bloqueados (
            user_id BIGINT PRIMARY KEY,
            motivo TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS eventos (
            id SERIAL PRIMARY KEY,
            nome TEXT,
            inicio TIMESTAMP,
            fim TIMESTAMP,
            multiplicador INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS palavras_proibidas (
            id SERIAL PRIMARY KEY,
            palavra TEXT UNIQUE NOT NULL
        );
    """)
    await conn.close()

# --- Helpers de usuário e pontuação ---

async def adicionar_usuario(user_id: int, username: str):
    conn = await asyncpg.connect(database='meubanco', user='meuuser', password='senha')
    await conn.execute(
        """
        INSERT INTO usuarios (user_id, username)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username;
        """,
        user_id, username
    )
    await conn.close()


async def obter_usuario(user_id: int):
    conn = await asyncpg.connect(database='meubanco', user='meuuser', password='senha')
    row = await conn.fetchrow("SELECT * FROM usuarios WHERE user_id = $1", user_id)
    await conn.close()
    return row


# Registra histórico de pontos
async def registrar_historico(user_id: int, pontos: int, motivo: str = None):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "INSERT INTO historico_pontos (user_id, pontos, motivo) VALUES ($1, $2, $3)",
            user_id, pontos, motivo
        )
    finally:
        await conn.close()


# Atualiza pontos do usuário
async def atualizar_pontos(
    user_id: int,
    delta: int,
    motivo: str = None,
    bot: Bot = None
) -> int | None:
    usuario = await obter_usuario(user_id)
    if not usuario:
        return None

    novos = usuario['pontos'] + delta
    ja_pontuador = usuario['is_pontuador']
    nivel = usuario['nivel_atingido']

    await registrar_historico(user_id, delta, motivo)

    # Pontuador e brinde
    becomes_pontuador = False
    if not ja_pontuador and novos >= LIMIAR_PONTUADOR:
        ja_pontuador = True
        nivel += 1
        becomes_pontuador = True

    brinde = None
    for limiar, nome in NIVEIS_BRINDES.items():
        if usuario['pontos'] < limiar <= novos:
            brinde = nome
            nivel += 1
            break

    # Atualiza no banco
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """
            UPDATE usuarios SET pontos = $1, nivel_atingido = $2, is_pontuador = $3
            WHERE user_id = $4
            """,
            novos, nivel, ja_pontuador, user_id
        )
    finally:
        await conn.close()

    # Mensagem opcional
    if becomes_pontuador and bot:
        await bot.send_message(
            chat_id=user_id,
            text="🎉 Parabéns! Você se tornou um pontuador!"
        )
    elif brinde and bot:
        await bot.send_message(
            chat_id=user_id,
            text=f"🎁 Você conquistou um novo brinde: *{brinde}*",
            parse_mode="Markdown"
        )

    return novos

    # Atualiza dados do usuário no banco
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE usuarios
                   SET pontos = %s,
                       is_pontuador = %s,
                       nivel_atingido = %s
                 WHERE user_id = %s
                """,
                (novos, ja_pontuador, nivel, user_id)
            )
        conn.commit()

    # Envia notificações ao admin, se houver bot
    if bot:
        texto_base = f"🔔 Usuário `{user_id}` atingiu {novos} pontos"
        if becomes_pontuador:
            texto = texto_base + " e virou *PONTUADOR*." + (f" Motivo: {motivo}" if motivo else "")
            asyncio.create_task(
                bot.send_message(chat_id=ID_ADMIN, text=texto, parse_mode='Markdown')
            )
        if brinde:
            texto = texto_base + f" e ganhou *{brinde}*." + (f" Motivo: {motivo}" if motivo else "")
            asyncio.create_task(
                bot.send_message(chat_id=ID_ADMIN, text=texto, parse_mode='Markdown')
            )

    return novos


# Mensagem de Mural de Entrada
async def setup_bot_description(app):
    try:
        # Descrição curta (topo da conversa)
        await app.bot.set_my_short_description(
            short_description="🤖 Olá! Sou um bot do @cupomnavitrine – Gerenciador de pontuação.",
            language_code="pt"
        )

        # Descrição longa (detalhes do bot)
        await app.bot.set_my_description(
            description="🤖 Se inscreva em nosso canal @cupomnavitrine para ganhar cupons e recompensas!",
            language_code="pt"
        )

        logger.info("✅ Descrições do bot definidas com sucesso.")
    except Exception:
        logger.exception("❌ Erro ao definir descrições do bot")


async def setup_commands(app):
    try:
        # Comandos padrão (públicos: grupos/canais)
        public_commands = [
            BotCommand("meus_pontos", "Ver sua pontuação e nível"),
            BotCommand("ranking_top10", "Top 10 de usuários por pontos"),
            BotCommand("historico", "Mostrar seu histórico de pontos"),
            BotCommand("como_ganhar", "Como ganhar mais pontos"),
        ]
        await app.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())

        # Comandos privados (usuários em chat direto com o bot)
        private_commands = public_commands + [
            BotCommand("suporte", "Enviar mensagem ao suporte"),
            BotCommand("cancelar", "Cancelar mensagem ao suporte"),
        ]
        await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())

        logger.info("✅ Comandos configurados para público e privado.")
    except Exception:
        logger.exception("❌ Erro ao configurar comandos")


ADMIN_MENU = (
    "🔧 *Menu Admin* 🔧\n\n"
    "/pontuar – Atribuir pontos a um usuário\n"
    "/add_pontuador – Tornar usuário pontuador\n"
    "/zerar_pontos – Zerar pontos\n"
    "/remover_pontuador – Remover permissão de pontuador\n"
    "/bloquear – Bloquear usuário\n"
    "/desbloquear – Desbloquear usuário\n"
    "/adapproibida – Adicionar palavra proibida\n"
    "/delproibida – Remover palavra proibida\n"
    "/listaproibida – Listar palavras proibidas\n"
)

# Handler principal para o /admin
async def iniciar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in ADMIN_IDS:
        context.user_data["is_admin"] = True
        await update.message.reply_markdown(ADMIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("🔒 Digite a senha de admin:")
    return AGUARDANDO_SENHA


# Verifica a senha digitada
async def tratar_senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    senha_digitada = update.message.text.strip()
    if senha_digitada == str(ADMIN_PASSWORD):
        context.user_data["is_admin"] = True
        await update.message.reply_markdown(ADMIN_MENU)
    else:
        await update.message.reply_text("❌ Senha incorreta. Acesso negado.")
    return ConversationHandler.END


# Obtém registro de bloqueio
async def obter_bloqueado(user_id: int) -> dict | None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT motivo FROM usuarios_bloqueados WHERE user_id = $1",
            user_id
        )
        return dict(row) if row else None
    finally:
        await conn.close()


# Adiciona palavra proibida
async def add_palavra_proibida_bd(palavra: str) -> bool:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await conn.execute(
            """
            INSERT INTO palavras_proibidas (palavra)
            VALUES ($1)
            ON CONFLICT DO NOTHING
            """,
            palavra.lower()
        )
        return result.endswith("INSERT 0 1")
    finally:
        await conn.close()


# Remove palavra proibida
async def del_palavra_proibida_bd(palavra: str) -> bool:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await conn.execute(
            "DELETE FROM palavras_proibidas WHERE palavra = $1",
            palavra.lower()
        )
        return result.endswith("DELETE 1")
    finally:
        await conn.close()


# Lista todas as palavras proibidas
async def listar_palavras_proibidas_db() -> list[str]:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT palavra FROM palavras_proibidas")
        return [row["palavra"] for row in rows]
    finally:
        await conn.close()

# --- Handlers de palavras proibidas ---

async def add_palavra_proibida(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    if not context.args:
        return await update.message.reply_text("Uso: /adapproibida <palavra>")
    palavra = context.args[0].lower()
    sucesso = await add_palavra_proibida_bd(palavra)  # <-- Assíncrona agora
    if sucesso:
        await update.message.reply_text(f"✅ Palavra '{palavra}' adicionada à lista proibida.")
    else:
        await update.message.reply_text(f"⚠️ Palavra '{palavra}' já está na lista.")


async def del_palavra_proibida(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    if not context.args:
        return await update.message.reply_text("Uso: /delproibida <palavra>")
    palavra = context.args[0].lower()
    sucesso = await del_palavra_proibida_bd(palavra)  # <-- Assíncrona agora
    if sucesso:
        await update.message.reply_text(f"🗑️ Palavra '{palavra}' removida da lista.")
    else:
        await update.message.reply_text(f"⚠️ Palavra '{palavra}' não encontrada.")


async def listar_palavras_proibidas(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    palavras = await listar_palavras_proibidas_db()  # <-- Assíncrona agora
    text = "Nenhuma" if not palavras else ", ".join(palavras)
    await update.message.reply_text(f"🔒 Palavras proibidas: {text}")

# --- Handler de suporte ---

async def suporte(update: Update, context: CallbackContext):
    await update.message.reply_text("📝 Escreva sua mensagem de suporte (máx. 500 caracteres). Use /cancelar para abortar.")
    return ESPERANDO_SUPORTE


async def receber_suporte(update: Update, context: CallbackContext):
    texto = update.message.text.strip()
    if len(texto) > 500:
        await update.message.reply_text("❌ Sua mensagem ultrapassa 500 caracteres. Tente novamente.")
        return ESPERANDO_SUPORTE
    for palavra in listar_palavras_proibidas_db():
        if palavra in texto.lower():
            await update.message.reply_text(
                "🚫 Sua mensagem contém uma palavra proibida. Revise e tente novamente."
            )
            return ESPERANDO_SUPORTE
    user = update.effective_user
    context.bot.send_message(
        chat_id=ID_ADMIN,
        text=f"📩 Suporte de {user.username or user.id}: {texto}",
        parse_mode='Markdown'
    )
    await update.message.reply_text("✅ Sua mensagem foi enviada. Obrigado!")
    return ConversationHandler.END


async def cancelar(update: Update, context: CallbackContext):
    await update.message.reply_text("❌ Suporte cancelado.")
    return ConversationHandler.END

# --- Handlers de comando existentes ---

# Handler para /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Olá! Bem-vindo ao Bot de Pontuação da Vitrine.\n\n"
        "Aqui você pode:\n"
        "• Ver seus pontos com /meus_pontos\n"
        "• Conferir o ranking com /ranking_top10\n"
        "• Ver seu histórico com /historico\n"
        "• Saber como ganhar pontos com /como_ganhar\n\n"
        "Basta clicar em um comando ou digitá-lo na conversa. Vamos começar?"
    )


async def meus_pontos(update: Update, context: CallbackContext):
    user = update.effective_user
    # garante que o usuário exista na base
    await adicionar_usuario(user.id, user.username)
    u = await obter_usuario(user.id)
    # envia a resposta normalmente
    await update.message.reply_text(
        f"Você tem {u['pontos']} pontos (Nível {u['nivel_atingido']})."
    )


async def como_ganhar(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "🎯 Você ganha pontos por:\n"
        "• Presença inicial no grupo\n"
        "• Ser promovido a pontuador (500 pts)\n"
        "• Receber pontuações de outros usuários\n\n"
        "Use /meus_pontos para ver seu total!"
    )


async def Atribuir_pontos(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = obter_usuario(chamador)
    if not reg or not reg['is_pontuador']:
        return await update.message.reply_text("🔒 Sem permissão para pontuar.")
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Uso: /pontuar <user_id> <pontos> [motivo]")
    try:
        alvo_id = int(args[0]); pts = int(args[1])
    except ValueError:
        return await update.message.reply_text("IDs e pontos devem ser números.")
    motivo = ' '.join(args[2:]) if len(args) > 2 else None
    if not obter_usuario(alvo_id):
        return await update.message.reply_text("Usuário não encontrado.")
    atualizar_pontos(alvo_id, pts, motivo, context.bot)
    await update.message.reply_text(f"✅ Atribuídos {pts} pontos ao usuário {alvo_id}.")


async def adicionar_pontuador(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = obter_usuario(chamador)
    if chamador != ID_ADMIN and not reg['is_pontuador']:
        return await update.message.reply_text("🔒 Sem permissão.")
    try:
        novo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /add_pontuador <user_id>")
    await (novo, None)
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET is_pontuador=TRUE WHERE user_id=%s", (novo,))
            conn.commit()
    await update.message.reply_text(f"✅ Usuário {novo} virou pontuador.")


async def historico(update: Update, context: CallbackContext):
    user = update.effective_user
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data,pontos,motivo FROM historico_pontos WHERE user_id=%s ORDER BY data DESC LIMIT 10",
                (user.id,)
            )
            rows = cur.fetchall()
    lines = [f"{r['data'].strftime('%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}" for r in rows]
    await update.message.reply_text("🗒️ Seu histórico:\n" + "\n".join(lines))


async def ranking_top10(update: Update, context: CallbackContext):
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username,pontos FROM usuarios ORDER BY pontos DESC LIMIT 10")
            top = cur.fetchall()
            text = "🏅 Top 10 de pontos:\n" + "\n".join(
                [f"{i + 1}. {(u['username'] or 'Usuário')} – {u['pontos']} pts" for i, u in enumerate(top)]
            )
            await update.message.reply_text(text)


async def zerar_pontos(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /zerar_pontos <user_id>")
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET pontos=0 WHERE user_id=%s", (alvo,))
            conn.commit()
    await update.message.reply_text(f"✅ Pontos de {alvo} zerados.")


async def pontuador(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /remover_pontuador <user_id>")
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET is_pontuador=FALSE WHERE user_id=%s", (alvo,))
            conn.commit()
    await update.message.reply_text(f"✅ {alvo} não é mais pontuador.")


async def tratar_presenca(update: Update, context: CallbackContext):
    user = update.effective_user
    await adicionar_usuario(user.id, user.username)
    reg = obter_usuario(user.id)
    if not reg['visto']:
        atualizar_pontos(user.id, 1, 'Presença inicial', context.bot)
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE usuarios SET visto=TRUE WHERE user_id=%s", (user.id,))
                conn.commit()
        await update.message.reply_text("👋 +1 ponto por participar!")


# --- Inicialização do bot ---
if __name__ == '__main__':
    init_db()
    logger.info(f"✅ Banco inicializado. BOT_TOKEN está definido? {'Sim' if BOT_TOKEN else 'Não'}")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_description)
        .post_init(setup_commands)
        .build()
    )

    # middleware de bloqueio e outros handlers registrados abaixo
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('meus_pontos', meus_pontos))
    app.add_handler(CommandHandler('como_ganhar', como_ganhar))

    # Administração e pontuação
    app.add_handler(CommandHandler('pontuar', Atribuir_pontos))
    app.add_handler(CommandHandler('add_pontuador', adicionar_pontuador))
    app.add_handler(CommandHandler('historico', historico))
    app.add_handler(CommandHandler('ranking_top10', ranking_top10))
    app.add_handler(CommandHandler('zerar_pontos', zerar_pontos))
    app.add_handler(CommandHandler('remover_pontuador', pontuador))
    app.add_handler(CommandHandler('bloquear', bloquear_usuario))
    app.add_handler(CommandHandler('desbloquear', desbloquear_usuario))
    app.add_handler(CommandHandler('adapproibida', add_palavra_proibida))
    app.add_handler(CommandHandler('delproibida', del_palavra_proibida))
    app.add_handler(CommandHandler('listaproibida', listar_palavras_proibidas))

    # Fluxo de admin
    admin_handler = ConversationHandler(
        entry_points=[CommandHandler('admin', iniciar_admin)],
        states={
            AGUARDANDO_SENHA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_senha)
            ],
        },
        fallbacks=[]
    )
    app.add_handler(admin_handler)


    # Conversa de suporte
    suporte_handler = ConversationHandler(
        entry_points=[CommandHandler('suporte', suporte)],
        states={ESPERANDO_SUPORTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_suporte)]},
        fallbacks=[CommandHandler('cancelar', cancelar)]
    )
    app.add_handler(suporte_handler)

    # Presença
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))

print("🔄 Iniciando polling...")  # <-- mensagem será exibida no console
try:
    app.run_polling()
except Exception as e:
    logger.exception("❌ Erro durante run_polling")