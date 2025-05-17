import os
import re
import sys
import asyncpg
import logging
from time import perf_counter
from asyncpg import UniqueViolationError, PostgresError, CannotConnectNowError, ConnectionDoesNotExistError
import asyncio
import csv
import io
import math
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import CallbackQueryHandler
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

ADMIN_IDS = os.getenv("ADMIN_IDS", "")
if ADMIN_IDS:
    try:
        ADMINS = [int(x.strip()) for x in ADMIN_IDS.split(',') if x.strip()]
    except ValueError:
        logger.error("ADMIN_IDS deve conter apenas números separados por vírgula.")
        ADMINS = []
else:
    ADMINS = []

# --- Estados de conversa ---
ADMIN_SENHA, ESPERANDO_SUPORTE, ADD_PONTOS_POR_ID, ADD_PONTOS_QTD, ADD_PONTOS_MOTIVO, \
DEL_PONTOS_ID, DEL_PONTOS_QTD, DEL_PONTOS_MOTIVO, REMOVER_PONTUADOR_ID, BLOQUEAR_ID, \
DESBLOQUEAR_ID, ADD_PALAVRA_PROIBIDA, DEL_PALAVRA_PROIBIDA = range(13)

TEMPO_LIMITE_BUSCA = 5          # Tempo máximo (em segundos) para consulta
PAGE_SIZE = 20             # Número de usuários por página


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL,min_size=1,max_size=10)
    async with pool.acquire() as conn:
        # Criação de tabelas se não existirem
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
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
async def adicionar_usuario_db(pool, user_id: int, username: str, first_name: str):
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1) tenta buscar registro antigo
            old = await conn.fetchrow(
                "SELECT username, first_name FROM usuarios WHERE user_id = $1",
                user_id
            )

            if old:
                # 2) upsert via UPDATE
                await conn.execute(
                    """
                    UPDATE usuarios
                       SET username   = $1,
                           first_name = $2
                     WHERE user_id = $3
                    """,
                    username, first_name, user_id
                )

                # 3) grava histórico de cada mudança
                if old['username'] != username:
                    await conn.execute(
                        """
                        INSERT INTO usuario_history
                          (user_id, campo, valor_antigo, valor_novo, criado_em)
                        VALUES ($1, 'username', $2, $3, now())
                        """,
                        user_id, old['username'], username
                    )
                if old['first_name'] != first_name:
                    await conn.execute(
                        """
                        INSERT INTO usuario_history
                          (user_id, campo, valor_antigo, valor_novo, criado_em)
                        VALUES ($1, 'first_name', $2, $3, now())
                        """,
                        user_id, old['first_name'], first_name
                    )

            else:
                # 4) registro novo
                await conn.execute(
                    """
                    INSERT INTO usuarios(user_id, username, first_name)
                    VALUES($1, $2, $3)
                    """,
                    user_id, username, first_name
                )
                # (opcional) histórico de novo usuário
                await conn.execute(
                    """
                    INSERT INTO usuario_history
                      (user_id, campo, valor_antigo, valor_novo, criado_em)
                    VALUES ($1, 'INSERT', NULL, $2, now())
                    """,
                    user_id, username
                )


async def obter_usuario_db(user_id: int) -> asyncpg.Record | None:
    """
    Retorna um registro de usuário como asyncpg.Record ou None.
    """
    return await pool.fetchrow(
        "SELECT * FROM usuarios WHERE user_id = $1",
        user_id
    )

async def registrar_historico_db(user_id: int, pontos: int, motivo: str | None = None):
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

async def total_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /total_usuarios
    Retorna o número total de usuários cadastrados na tabela 'usuarios'.
    Somente admins podem executar.
    """
    # 1) Verifica permissão
    requester_id = update.effective_user.id
    if requester_id not in ADMINS:
        await update.message.reply_text("❌ Você não tem permissão para isso.")
        return

    # 2) Consulta ao banco com timeout
    try:
        total = await asyncio.wait_for(
            pool.fetchval("SELECT COUNT(*) FROM usuarios"),
            timeout=TEMPO_LIMITE_BUSCA
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "❌ A consulta demorou demais. Tente novamente mais tarde."
        )
        return
    except Exception as e:
        logger.error("Erro ao contar usuários: %s", e)
        await update.message.reply_text(
            "❌ Erro ao acessar o banco. Tente novamente mais tarde."
        )
        return

    # 3) Envia resultado
    await update.message.reply_text(f"👥 Total de usuários cadastrados: {total}")


async def atualizar_pontos(
    user_id: int,
    delta: int,
    motivo: str = None,
    bot: Bot = None
) -> int | None:
    # Busca usuário (async)
    usuario = await obter_usuario_db(user_id)
    if not usuario:
        return None

    # Calcula novos pontos e nível
    novos = usuario['pontos'] + delta
    ja_pontuador = usuario['is_pontuador']
    nivel = usuario['nivel_atingido']

    # Registra histórico (async)
    await registrar_historico_db(user_id, delta, motivo)

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
        usuario = await obter_usuario_db(user_id)
        if not usuario:
            return None

        # Calcula novos pontos e nível
        novos = usuario['pontos'] + delta
        ja_pontuador = usuario['is_pontuador']
        nivel = usuario['nivel_atingido']

        # Registra histórico (async)
        await registrar_historico_db(user_id, delta, motivo)

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


def escape_markdown_v2(text: str) -> str:
    """
    Escapa caracteres reservados do MarkdownV2.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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
            BotCommand("ranking_top10q", "Top 10 dos ultimos 15 dias"),
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
    "/add_pontos – Atribuir pontos a um usuário\n"
    "/del_pontos – remover pontos de um usuário\n"
    "/add_admin – adicionar novo admin\n"
    "/rem_admin – remover admin\n"
    "/rem_pontuador – Remover permissão de pontuador\n"
    "/bloquear – Bloquear usuário\n"
    "/desbloquear – Desbloquear usuário\n"
    "/historico_usuario – historico de nomes do usuário\n"
    "/listar_usuarios – lista de usuarios cadastrados\n"
    "/total_usuarios – quantidade total de usuarios cadastrados\n"
    "/adapproibida – Adicionar palavra proibida\n"
    "/delproibida – Remover palavra proibida\n"
    "/listaproibida – Listar palavras proibidas\n"
)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMINS:
        context.user_data["is_admin"] = True
        await update.message.reply_text(ADMIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("🔒 Digite a senha de admin:")
    return ADMIN_SENHA

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


async def cancelar_suporte(update: Update, context: CallbackContext):
    await update.message.reply_text("❌ Suporte cancelado.")
    return ConversationHandler.END


# Handler para /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # dispara em background ou await, como preferir
    asyncio.create_task(
        adicionar_usuario_db(
            pool,
            user.id,
            user.username or "",
            user.first_name or ""
        )
    )

    await update.message.reply_text(
        "🤖 Olá! Bem-vindo ao Bot de Pontuação da @cupomnavitrine.\n\n"
        "Para começar abra o menu lateral ou digite um comando."
    )

async def meus_pontos(update: Update, context: CallbackContext):
    """
    Exibe ao usuário seus pontos e nível atual,
    tratando possíveis falhas de conexão ao banco.
    """
    user = update.effective_user

    try:
        # Tenta inserir ou atualizar o usuário
        await adicionar_usuario_db(user.id, user.username)
        # Tenta buscar os dados de pontos e nível
        u = await obter_usuario_db(user.id)

    except Exception as e:
        # Aqui você pode usar logger.error(e) para registrar a stack
        await update.message.reply_text(
            "❌ Desculpe, tivemos um problema ao acessar as suas informações. Tente novamente mais tarde. se o problema persistir contate o suporte."
        )
        return

    # Se tudo ocorreu bem, respondemos normalmente
    await update.message.reply_text(
        f"🎉 Você tem {u['pontos']} pontos (Nível {u['nivel_atingido']})."
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


async def add_pontos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
        Inicia o fluxo de atribuição de pontos.
        Verifica se o usuário possui permissão temporária (senha válida) e
        pergunta qual é o user_id que receberá pontos.
        """
    # Verifica permissão temporária
    if not context.user_data.get("is_admin"):
        return await update.message.reply_text("🔒 Você precisa autenticar como admin primeiro.")
    # Inicia a conversa
    await update.message.reply_text("📋 Atribuir pontos: primeiro, qual é o user_id?")
    return ADD_PONTOS_POR_ID

async def add_pontos_IDuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
        Recebe o ID do usuário destinatário.
        Valida se é um número inteiro. Se válido, armazena em user_data
        e pergunta a quantidade de pontos.
        """
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("❗️ ID inválido. Digite somente números para o user_id.")
    context.user_data["add_pt_id"] = int(text)
    await update.message.reply_text("✏️ Quantos pontos você quer dar?")
    return ADD_PONTOS_POR_ID

async def add_pontos_quantidade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
        Recebe a quantidade de pontos a ser atribuída.
        Valida se é número inteiro. Se válido, armazena em user_data
        e pergunta o motivo da atribuição.
        """
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("❗️ Valor inválido. Digite somente números para os pontos.")
    context.user_data["add_pt_value"] = int(text)
    await update.message.reply_text("📝 Por fim, qual o motivo para registrar no histórico?")
    return ADD_PONTOS_POR_ID

async def add_pontos_motivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
        Recebe o motivo da atribuição de pontos.
        Valida se não está vazio. Se válido, recupera todos os dados de user_data,
        verifica a existência do usuário, faz a atualização cumulativa no banco
        e envia a confirmação. Encerra a conversa.
        """
    motivo = update.message.text.strip()
    if not motivo:
        return await update.message.reply_text("❗️ Motivo não pode ficar em branco. Digite um texto.")
    context.user_data["add_pt_reason"] = motivo

    # Todos os dados coletados, processa a atualização
    alvo_id = context.user_data.pop("add_pt_id")
    pontos  = context.user_data.pop("add_pt_valOR")
    motivo  = context.user_data.pop("add_pt_motivo")

    usuario = await obter_usuario_db(alvo_id)
    if not usuario:
        return await update.message.reply_text("❌ Usuário não encontrado. Cancelando operação.")

    novo_total = await atualizar_pontos(alvo_id, pontos, motivo, context.bot)
    await update.message.reply_text(
        f"✅ {pontos} pts atribuídos a {alvo_id}.\n"
        f"Motivo: {motivo}\n"
        f"Total agora: {novo_total} pts."
    )
    return ConversationHandler.END

async def add_pontos_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
      Handler de fallback para cancelar o fluxo de atribuição de pontos.
      Envia mensagem de cancelamento e encerra a conversa.
      """
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END



async def adicionar_pontuador(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = await obter_usuario_db(chamador)
    if chamador != ID_ADMIN and not reg['is_pontuador']:
        return await update.message.reply_text("🔒 Sem permissão.")

    if not context.args:
        return await update.message.reply_text("Uso: /add_pontuador <user_id>")
    try:
        novo = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Uso: /add_pontuador <user_id>")

    await adicionar_usuario_db(novo, None)
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
         LIMIT 25
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
    await adicionar_usuario_db(user.id, user.username)
    reg = await obter_usuario_db(user.id)

    if not reg['visto']:
        # pontua presença
        await atualizar_pontos(user.id, 1, 'Presença inicial', context.bot)
        # marca visto = TRUE
        await pool.execute(
            "UPDATE usuarios SET visto = TRUE WHERE user_id = $1",
            user.id
        )
        await update.message.reply_text("👋 +1 ponto por participar!")

async def cancelar(update: Update, conText: ContextTypes.DEFAULT_TYPE):
    # Limpa tudo que já havia sido armazenado
    conText.user_data.clear()
    await update.message.reply_text(
        "❌ Operação cancelada. Nenhum dado foi salvo."
    )
    return ConversationHandler.END

PAGE_SIZE = 20
MAX_MESSAGE_LENGTH = 4000

async def historico_usuario(update: Update, context: CallbackContext):
    """
    /historico_usuario <user_id> [page]
    Exibe mudanças de username/first_name de um usuário paginadas.
    Somente admins podem executar.
    """
    AJUDA_HISTORICO = (
        "*📘 Ajuda: /historico_usuario*\n\n"
        "Este comando permite visualizar o histórico de alterações de *nome* ou *username* de um usuário.\n\n"
        "*Uso básico:*\n"
        "`/historico_usuario <user_id>` – Mostra a 1ª página do histórico do usuário informado.\n\n"
        "*Uso com paginação:*\n"
        "`/historico_usuario <user_id> <página>` – Mostra a página desejada do histórico.\n\n"
        "*Exemplos:*\n"
        "`/historico_usuario 123456789` – Exibe as alterações recentes do usuário com ID 123456789.\n"
        "`/historico_usuario 123456789 2` – Exibe a 2ª página do histórico.\n\n"
        "*ℹ️ Observações:*\n"
        "• Apenas administradores têm permissão para executar este comando.\n"
        f"• Cada página exibe até *{PAGE_SIZE}* registros.\n"
        "• As alterações são registradas automaticamente sempre que um nome ou username muda.\n"
    )
    # 1) Permissão
    requester_id = update.effective_user.id
    if requester_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Você não tem permissão para isso.")
        return

    # Verificação de argumentos
    args = context.args or []
    if len(args) not in (1, 2) or not args[0].isdigit():
        await update.message.reply_text(AJUDA_HISTORICO, parse_mode="MarkdownV2")
        return

    # 2) Validação de args e paginação
    args = context.args or []
    if len(args) not in (1, 2) or not args[0].isdigit():
        await update.message.reply_text("Use: /historico_usuario <user_id> [page]")
        return

    target_id = int(args[0])
    page = int(args[1]) if len(args) == 2 and args[1].isdigit() and int(args[1]) > 0 else 1
    offset = (page - 1) * PAGE_SIZE

    # 3) Fetch com tratamento de erros e timeout
    try:
        rows = await pool.fetch(
            """
            SELECT campo, valor_antigo, valor_novo, changed_at
              FROM usuario_history
             WHERE user_id = $1
          ORDER BY changed_at DESC
             LIMIT $2 OFFSET $3
            """,
            target_id, PAGE_SIZE, offset
        )
    except (asyncpg.CannotConnectNowError,
            asyncpg.ConnectionDoesNotExistError,
            asyncpg.PostgresError) as db_err:
        logger.error("Erro ao buscar histórico de %s (page %d): %s", target_id, page, db_err)
        await update.message.reply_text(
            "❌ Não foi possível acessar o histórico no momento. Tente novamente mais tarde."
        )
        return
    except Exception:
        logger.exception("Erro inesperado ao buscar histórico de %s (page %d)", target_id, page)
        await update.message.reply_text(
            "❌ Ocorreu um erro inesperado. Tente novamente mais tarde."
        )
        return

    # 4) Sem registros
    if not rows:
        await update.message.reply_text(
            f"ℹ️ Sem histórico de alterações para o user_id `{target_id}` na página {page}."
        )
        return

    # 5) Monta texto
    header = f"🕒 Histórico de alterações para `{target_id}` (página {page}, {PAGE_SIZE} por página):"
    lines = [header]
    for r in rows:
        ts = r["changed_at"].strftime("%d/%m/%Y %H:%M")
        campo = escape_markdown_v2(r["campo"])
        antigo = escape_markdown_v2(r["antigo"])
        novo = escape_markdown_v2(r["novo"])

        lines.append(f"{ts} — *{campo}*: `{antigo}` → `{novo}`")

    texto = "\n".join(lines)

    # 6) Divide em blocos se muito longo
    if len(texto) > MAX_MESSAGE_LENGTH:
        chunks = []
        current = []
        size = 0
        for line in lines:
            size += len(line) + 1
            if size > MAX_MESSAGE_LENGTH:
                chunks.append("\n".join(current))
                current = [line]
                size = len(line) + 1
            else:
                current.append(line)
        if current:
            chunks.append("\n".join(current))

        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(texto, parse_mode="MarkdownV2")

    # 7) Log de acesso para auditoria
    logger.info("Admin %s consultou histórico do user %s (page %d)", requester_id, target_id, page)

async def listar_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /listar_usuarios [page | nome <prefixo> | id <prefixo>]
    Mostra lista de usuários com paginação e filtros opcionais.
    - page: número da página (padrão 1)
    - nome <prefixo>: filtra names começando com prefixo
    - id <prefixo>: filtra IDs começando com prefixo
    Somente ADMINS podem executar.
    """
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        return await update.message.reply_text("❌ Você não tem permissão para isso.")

    args = context.args or []
    page = 1
    filtro_sql = ""
    filtro_args = []

    # Interpretar subcomandos de filtro
    if args:
        if args[0].isdigit():
            page = max(1, int(args[0]))
        elif args[0].lower() == 'nome' and len(args) > 1:
            prefix = args[1]
            filtro_sql = "WHERE first_name ILIKE $1"
            filtro_args = [f"{prefix}%"]
        elif args[0].lower() == 'id' and len(args) > 1:
            prefix = args[1]
            filtro_sql = "WHERE CAST(user_id AS TEXT) LIKE $1"
            filtro_args = [f"{prefix}%"]

    try:
        # Conta total de registros para paginação
        total = await asyncio.wait_for(
            pool.fetchval(f"SELECT COUNT(*) FROM usuarios {filtro_sql}", *filtro_args),
            timeout=TEMPO_LIMITE_BUSCA
        )
    except Exception as e:
        logger.error("Erro ao contar usuários: %s", e)
        return await update.message.reply_text("❌ Erro ao acessar o banco.")

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    if page > total_pages:
        return await update.message.reply_text(
            f"ℹ️ Página {page} não existe. Só até {total_pages}."
        )

    offset = (page - 1) * PAGE_SIZE
    query = (
        f"SELECT user_id, first_name FROM usuarios {filtro_sql} "
        f"ORDER BY user_id LIMIT $1 OFFSET $2"
    )

    try:
        rows = await asyncio.wait_for(
            pool.fetch(query, *(filtro_args + [PAGE_SIZE, offset])),
            timeout=TEMPO_LIMITE_BUSCA
        )
    except asyncio.TimeoutError:
        return await update.message.reply_text(
            "❌ A consulta demorou demais. Tente novamente mais tarde."
        )
    except Exception as e:
        logger.error("Erro ao listar usuários: %s", e)
        return await update.message.reply_text(
            "❌ Erro ao acessar o banco. Tente novamente mais tarde."
        )

    if not rows:
        return await update.message.reply_text("ℹ️ Nenhum usuário encontrado.")

    # Monta mensagem e botões de navegação
    header = f"🗒️ Usuários (página {page}/{total_pages}, total {total}):"
    lines = [header]
    for r in rows:
        nome = r['first_name'] or '<sem nome>'
        lines.append(f"• `{r['user_id']}` — {nome}")
    text = "\n".join(lines)

    # Inline keyboard para navegar páginas
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton('◀️ Anterior', callback_data=f"usuarios|{page-1}|{' '.join(args)}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton('Próxima ▶️', callback_data=f"usuarios|{page+1}|{' '.join(args)}"))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

    await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=keyboard)


async def callback_listar_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback para navegação da listagem de usuários.
    """
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    _, page_str, args_str = data
    # Reconstrói contexto e chama listar_usuarios
    context.args = args_str.split() if args_str else []
    # Simula update.message para reaproveitar a função
    return await listar_usuarios(query, context)


async def exportar_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /exportar_usuarios
    Exporta todos os usuários em arquivo CSV.
    Somente ADMINS podem executar.
    """
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        return await update.message.reply_text("❌ Você não tem permissão para isso.")

    try:
        rows = await asyncio.wait_for(
            pool.fetch("SELECT user_id, first_name FROM usuarios ORDER BY user_id"),
            timeout=TEMPO_LIMITE_BUSCA
        )
    except Exception as e:
        logger.error("Erro ao exportar usuários: %s", e)
        return await update.message.reply_text("❌ Erro ao acessar o banco.")

    if not rows:
        return await update.message.reply_text("ℹ️ Nenhum usuário para exportar.")

    # Cria CSV em memória
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['user_id', 'first_name'])
    for r in rows:
        writer.writerow([r['user_id'], r['first_name'] or ''])
    buffer.seek(0)

    # Envia como arquivo
    file = InputFile(buffer, filename='usuarios.csv')
    await update.message.reply_document(document=file, filename='usuarios.csv')


async def on_startup(app):
    # será executado no mesmo loop do Application
    await init_db_pool()
    logger.info("✅ Pool asyncpg inicializado e tabelas garantidas.")


main_conv = ConversationHandler(
    entry_points=[
        CommandHandler("admin", admin),
        CommandHandler("add_pontos", add_pontos),
        # CommandHandler("del_pontos", del_pontos),
        # CommandHandler("add_admin", add_admin),
        # CommandHandler("rem_admin", rem_admin),
        # CommandHandler("rem_pontuador", rem_pontuador),
        CommandHandler("historico_usuario", historico_usuario),
        # CommandHandler("listar_usuarios", listar_usuarios),
        # CommandHandler("total_usuarios", total_usuarios),
        # CommandHandler("bloquear", bloquear),
        # CommandHandler("desbloquear", desbloquear),
        CommandHandler("add_palavra_proibida", add_palavra_proibida),
        CommandHandler("del_palavra_proibida", del_palavra_proibida),
        # CommandHandler("listaproibida", listaproibida)
    ],
    states={
        # SENHA ADMIN (exemplo de validação inicial)
        ADMIN_SENHA: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_senha),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],

        # # SUPORTE (caso você use suporte)
        # ESPERANDO_SUPORTE: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_mensagem_suporte),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /add_pontos
        # ADD_PONTOS_POR_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_id_add_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # ADD_PONTOS_QTD: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_qtd_add_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # ADD_PONTOS_MOTIVO: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_motivo_add_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /del_pontos
        # DEL_PONTOS_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_id_del_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # DEL_PONTOS_QTD: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_qtd_del_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # DEL_PONTOS_MOTIVO: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_motivo_del_pontos),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /remover_pontuador
        # REMOVER_PONTUADOR_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_id_remover_pontuador),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /bloquear
        # BLOQUEAR_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_id_bloquear),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /desbloquear
        # DESBLOQUEAR_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_id_desbloquear),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /add_palavra_proibida
        # ADD_PALAVRA_PROIBIDA: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_palavras_proibidas_add),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        #
        # # /del_palavra_proibida
        # DEL_PALAVRA_PROIBIDA: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_palavras_proibidas_del),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
)

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
    app.add_handler(main_conv)

    app.add_handler(CommandHandler('start', start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('admin', admin, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('meus_pontos', meus_pontos))
    app.add_handler(CommandHandler('como_ganhar', como_ganhar))
    app.add_handler(CommandHandler('historico', historico))
    app.add_handler(CommandHandler('ranking_top10', ranking_top10))
    #app.add_handler(CommandHandler('ranking_top10q', ranking_top10q))

    # Presença em grupos
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))

    logger.info("🔄 Iniciando polling...")
    try:
        app.run_polling()
    except Exception:
        logger.exception("❌ Erro durante run_polling")