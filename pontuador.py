import os
import logging
import sys
import asyncpg
from telegram.ext import ApplicationHandlerStop
from telegram import Update, Bot
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, ContextTypes
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats
from telegram.ext import (
    CommandHandler, CallbackContext,
    MessageHandler, filters, ConversationHandler
)
pool: asyncpg.Pool | None = None
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# --- Configuração e constantes ---
BOT_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not BOT_TOKEN:
    logger.error("TELEGRAM_TOKEN não encontrado.")
    sys.exit(1)

DATABASE_URL = os.getenv('DATABASE_URL')
ID_ADMIN = int(os.getenv('ID_ADMIN'))
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


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL,min_size=1,max_size=10)
    async with pool.acquire() as conn:
        # Criação de tabelas se não existirem
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

# --- Helpers de usuário (asyncpg) ---
async def adicionar_usuario(user_id: int, username: str):
    """
    Insere ou atualiza um usuário na tabela 'usuarios'.
    """
    await pool.execute(
        """
        INSERT INTO usuarios (user_id, username)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE
          SET username = EXCLUDED.username
        """,
        user_id, username
    )

async def obter_usuario(user_id: int) -> asyncpg.Record | None:
    """
    Retorna um registro de usuário como asyncpg.Record ou None.
    """
    return await pool.fetchrow(
        "SELECT * FROM usuarios WHERE user_id = $1",
        user_id
    )

async def registrar_historico(user_id: int, pontos: int, motivo: str | None = None):
    """
    Insere um registro de pontos no histórico.
    """
    await pool.execute(
        """
        INSERT INTO historico_pontos (user_id, pontos, motivo)
        VALUES ($1, $2, $3)
        """,
        user_id, pontos, motivo
    )

async def atualizar_pontos(
    user_id: int,
    delta: int,
    motivo: str = None,
    bot: Bot = None
) -> int | None:
    # Busca usuário (async)
    usuario = await obter_usuario(user_id)
    if not usuario:
        return None

    # Calcula novos pontos e nível
    novos = usuario['pontos'] + delta
    ja_pontuador = usuario['is_pontuador']
    nivel = usuario['nivel_atingido']

    # Registra histórico (async)
    await registrar_historico(user_id, delta, motivo)

    # Verifica se virou pontuador
    becomes_pontuador = False
    if not ja_pontuador and novos >= LIMIAR_PONTUADOR:
        ja_pontuador = True
        nivel += 1
        becomes_pontuador = True

    # Verifica brinde por nível
    brinde = None
    for limiar, nome in NIVEIS_BRINDES.items():
        if usuario['pontos'] < limiar <= novos:
            brinde = nome
            nivel += 1
            break

    async def atualizar_pontos(
            user_id: int,
            delta: int,
            motivo: str | None = None,
            bot: Bot | None = None
    ) -> int | None:
        # Busca usuário (async)
        usuario = await obter_usuario(user_id)
        if not usuario:
            return None

        # Calcula novos pontos e nível
        novos = usuario['pontos'] + delta
        ja_pontuador = usuario['is_pontuador']
        nivel = usuario['nivel_atingido']

        # Registra histórico (async)
        await registrar_historico(user_id, delta, motivo)

        # Verifica se virou pontuador
        becomes_pontuador = False
        if not ja_pontuador and novos >= LIMIAR_PONTUADOR:
            ja_pontuador = True
            nivel += 1
            becomes_pontuador = True

        # Verifica brinde por nível
        brinde = None
        for limiar, nome in NIVEIS_BRINDES.items():
            if usuario['pontos'] < limiar <= novos:
                brinde = nome
                nivel += 1
                break

        # Atualiza dados do usuário no banco (asyncpg)
        await pool.execute(
            """
            UPDATE usuarios
               SET pontos = $1,
                   is_pontuador = $2,
                   nivel_atingido = $3
             WHERE user_id = $4
            """,
            novos, ja_pontuador, nivel, user_id
        )

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
        await app.bot.set_my_short_description(
            short_description="🤖 Olá! Sou um bot do @cupomnavitrine – Gerenciador de pontuação.",
            language_code="pt"
        )
        await app.bot.set_my_description(
            description="🤖 Se inscreva em nosso canal @cupomnavitrine –",
            language_code="pt"
        )
        logger.info("Descrições do bot definidas com sucesso.")
    except Exception:
        logger.exception("Erro ao definir descrições do bot")


async def setup_commands(app):
    try:
        comandos_basicos = [
            BotCommand("meus_pontos", "Ver sua pontuação e nível"),
            BotCommand("ranking_top10", "Top 10 de usuários por pontos"),
            BotCommand("historico", "Mostrar seu histórico de pontos"),
            BotCommand("como_ganhar", "Como ganhar mais pontos"),
        ]

        # 1) Comandos padrão (público)
        await app.bot.set_my_commands(
            comandos_basicos,
            scope=BotCommandScopeDefault()
        )

        # 2) Comandos em chat privado (com suporte)
        comandos_privados = comandos_basicos + [
            BotCommand("suporte", "Enviar mensagem ao suporte"),
            BotCommand("cancelar", "Cancelar mensagem ao suporte"),
        ]

        await app.bot.set_my_commands(
            comandos_privados,
            scope=BotCommandScopeAllPrivateChats()
        )

        logger.info("Comandos configurados para público e privado.")
    except Exception:
        logger.exception("Erro ao configurar comandos")


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

async def iniciar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        context.user_data["is_admin"] = True
        await update.message.reply_text(ADMIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("🔒 Digite a senha de admin:")
    return AGUARDANDO_SENHA

async def tratar_senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == str(ADMIN_PASSWORD):
        context.user_data["is_admin"] = True
        await update.message.reply_text(ADMIN_MENU)
    else:
        await update.message.reply_text("❌ Senha incorreta. Acesso negado.")
    return ConversationHandler.END

# --- Helpers de bloqueio com asyncpg ---

async def bloquear_user_bd(user_id: int, motivo: str = None):
    await pool.execute(
        """
        INSERT INTO usuarios_bloqueados (user_id, motivo)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET motivo = EXCLUDED.motivo, data = CURRENT_TIMESTAMP
        """,
        user_id, motivo
    )

async def desbloquear_user_bd(user_id: int):
    await pool.execute(
        "DELETE FROM usuarios_bloqueados WHERE user_id = $1",
        user_id
    )

async def obter_bloqueado(user_id: int):
    return await pool.fetchrow(
        "SELECT motivo FROM usuarios_bloqueados WHERE user_id = $1",
        user_id
    )


# --- Helpers de palavras proibidas (asyncpg) ---
async def add_palavra_proibida_bd(palavra: str) -> bool:
    """
    Tenta inserir uma palavra proibida; retorna True se inseriu, False se já existia.
    """
    # Usamos RETURNING para saber se houve inserção
    row = await pool.fetchrow(
        """
        INSERT INTO palavras_proibidas (palavra)
        VALUES ($1)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        palavra.lower()
    )
    return bool(row)


async def del_palavra_proibida_bd(palavra: str) -> bool:
    """
    Remove uma palavra proibida; retorna True se removeu, False se não encontrou.
    """
    # RETURNING nos dá feedback imediato
    row = await pool.fetchrow(
        """
        DELETE FROM palavras_proibidas
        WHERE palavra = $1
        RETURNING id
        """,
        palavra.lower()
    )
    return bool(row)


async def listar_palavras_proibidas_db() -> list[str]:
    """
    Retorna a lista de todas as palavras proibidas.
    """
    rows = await pool.fetch("SELECT palavra FROM palavras_proibidas")
    return [r["palavra"] for r in rows]


# --- Middleware de verificação de bloqueio ---
async def checar_bloqueio(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    reg = await obter_bloqueado(user_id)   # agora await
    if reg:
        motivo = reg['motivo'] or 'sem motivo especificado'
        await update.message.reply_text(f"⛔ Você está bloqueado. Motivo: {motivo}")
        raise ApplicationHandlerStop()


# --- Handlers de bloqueio ---
async def bloquear_usuario(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /bloquear <user_id> [motivo]")
    motivo = ' '.join(context.args[1:]) or None
    await bloquear_user_bd(alvo, motivo)   # await aqui
    await update.message.reply_text(f"✅ Usuário {alvo} bloqueado. Motivo: {motivo or 'nenhum'}")


async def desbloquear_usuario(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /desbloquear <user_id>")
    await desbloquear_user_bd(alvo)          # await aqui
    await update.message.reply_text(f"✅ Usuário {alvo} desbloqueado.")


# --- Handlers de palavras proibidas ---

async def add_palavra_proibida(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    if not context.args:
        return await update.message.reply_text("Uso: /adapproibida <palavra>")
    palavra = context.args[0].lower()
    if await add_palavra_proibida_bd(palavra):  # await aqui
        await update.message.reply_text(f"✅ Palavra '{palavra}' adicionada à lista proibida.")
    else:
        await update.message.reply_text(f"⚠️ Palavra '{palavra}' já está na lista.")


async def del_palavra_proibida(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    if not context.args:
        return await update.message.reply_text("Uso: /delproibida <palavra>")
    palavra = context.args[0].lower()
    if await del_palavra_proibida_bd(palavra):  # await aqui
        await update.message.reply_text(f"🗑️ Palavra '{palavra}' removida da lista.")
    else:
        await update.message.reply_text(f"⚠️ Palavra '{palavra}' não encontrada.")


async def listar_palavras_proibidas(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    palavras = await listar_palavras_proibidas_db()  # await aqui
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

    palavras = await listar_palavras_proibidas_db()  # await aqui
    for palavra in palavras:
        if palavra in texto.lower():
            await update.message.reply_text(
                "🚫 Sua mensagem contém uma palavra proibida. Revise e tente novamente."
            )
            return ESPERANDO_SUPORTE

    user = update.effective_user
    await context.bot.send_message(  # await aqui
        chat_id=ID_ADMIN,
        text=f"📩 Suporte de {user.username or user.id}: {texto}",
        parse_mode='Markdown'
    )
    await update.message.reply_text("✅ Sua mensagem foi enviada. Obrigado!")
    return ConversationHandler.END


async def cancelar(update: Update, context: CallbackContext):
    await update.message.reply_text("❌ Suporte cancelado.")
    return ConversationHandler.END


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
    await adicionar_usuario(user.id, user.username)
    u = await obter_usuario(user.id)
    await update.message.reply_text(
        f"Você tem {u['pontos']} pontos (Nível {u['nivel_atingido']})."
    )


async def como_ganhar(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "🎯 Você Pode Ganha Pontos Por:\n\n"
        "• Compras por ID em videos.\n"
        "• Até 1 comentário diario em grupos\n"
        "• Indicar links de lives com moedas\n"
        "• Receber pontuações de outros usuários por ajuda\n"
        "• Receber pontuações por convites ⓘ\n"
        "• Mais em Breve. \n\n"
        "Use /meus_pontos para ver seu total!"
    )


async def Atribuir_pontos(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = await obter_usuario(chamador)
    if not reg or not reg['is_pontuador']:
        return await update.message.reply_text("🔒 Sem permissão para pontuar.")

    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Uso: /pontuar <user_id> <pontos> [motivo]")

    try:
        alvo_id = int(args[0])
        pts = int(args[1])
    except ValueError:
        return await update.message.reply_text("IDs e pontos devem ser números.")

    motivo = ' '.join(args[2:]) if len(args) > 2 else None
    if not await obter_usuario(alvo_id):
        return await update.message.reply_text("Usuário não encontrado.")

    await atualizar_pontos(alvo_id, pts, motivo, context.bot)
    await update.message.reply_text(f"✅ Atribuídos {pts} pontos ao usuário {alvo_id}.")


async def adicionar_pontuador(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = await obter_usuario(chamador)
    if chamador != ID_ADMIN and not reg['is_pontuador']:
        return await update.message.reply_text("🔒 Sem permissão.")

    if not context.args:
        return await update.message.reply_text("Uso: /add_pontuador <user_id>")
    try:
        novo = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Uso: /add_pontuador <user_id>")

    await adicionar_usuario(novo, None)
    await pool.execute(
        "UPDATE usuarios SET is_pontuador = TRUE WHERE user_id = $1",
        novo
    )
    await update.message.reply_text(f"✅ Usuário {novo} virou pontuador.")


async def historico(update: Update, context: CallbackContext):
    user = update.effective_user
    rows = await pool.fetch(
        """
        SELECT data, pontos, motivo
          FROM historico_pontos
         WHERE user_id = $1
      ORDER BY data DESC
         LIMIT 10
        """,
        user.id
    )
    if not rows:
        await update.message.reply_text("🗒️ Nenhum registro de histórico encontrado.")
        return
    lines = [
        f"{r['data'].strftime('%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}"
        for r in rows
    ]
    await update.message.reply_text("🗒️ Seu histórico:\n" + "\n".join(lines))


async def ranking_top10(update: Update, context: CallbackContext):
    top = await pool.fetch(
        "SELECT username, pontos FROM usuarios ORDER BY pontos DESC LIMIT 10"
    )
    if not top:
        await update.message.reply_text("🏅 Nenhum usuário cadastrado.")
        return
    text = "🏅 Top 10 de pontos:\n" + "\n".join(
        f"{i + 1}. {(u['username'] or 'Usuário')} – {u['pontos']} pts"
        for i, u in enumerate(top)
    )
    await update.message.reply_text(text)


async def zerar_pontos(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Uso: /zerar_pontos <user_id>")

    await pool.execute(
        "UPDATE usuarios SET pontos = 0 WHERE user_id = $1",
        alvo
    )
    await update.message.reply_text(f"✅ Pontos de {alvo} zerados.")


async def pontuador(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Uso: /remover_pontuador <user_id>")

    await pool.execute(
        "UPDATE usuarios SET is_pontuador = FALSE WHERE user_id = $1",
        alvo
    )
    await update.message.reply_text(f"✅ {alvo} não é mais pontuador.")


async def tratar_presenca(update: Update, context: CallbackContext):
    user = update.effective_user
    # garante existência e recupera dados
    await adicionar_usuario(user.id, user.username)
    reg = await obter_usuario(user.id)

    if not reg['visto']:
        # pontua presença
        await atualizar_pontos(user.id, 1, 'Presença inicial', context.bot)
        # marca visto = TRUE
        await pool.execute(
            "UPDATE usuarios SET visto = TRUE WHERE user_id = $1",
            user.id
        )
        await update.message.reply_text("👋 +1 ponto por participar!")


async def on_startup(app):
    # será executado no mesmo loop do Application
    await init_db_pool()
    logger.info("✅ Pool asyncpg inicializado e tabelas garantidas.")


# --- Inicialização do bot ---
if __name__ == '__main__':
    import asyncio

    # 1) inicializa o pool ANTES de criar o Application
    asyncio.get_event_loop().run_until_complete(init_db_pool())
    logging.info("✅ Pool asyncpg inicializado e tabelas garantidas.")

    # 2) agora monte o bot
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_description)
        .post_init(setup_commands)
        .build()
    )
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
        states={AGUARDANDO_SENHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_senha)]},
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

    # Presença em grupos
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))

    logger.info("🔄 Iniciando polling...")
    try:
        app.run_polling()
    except Exception:
        logger.exception("❌ Erro durante run_polling")