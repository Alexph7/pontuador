import os
import re
import sys
import asyncpg
import logging
import itertools
import nest_asyncio
from operator import attrgetter  # ou itemgetter
from time import perf_counter
from asyncpg import UniqueViolationError, PostgresError, CannotConnectNowError, ConnectionDoesNotExistError
import asyncio
import csv
import io
import math
import datetime
from zoneinfo import ZoneInfo
from datetime import datetime, date
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram import Update, Bot
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler
from typing import Dict, Any
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, ContextTypes
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats
from telegram.ext import (
    CommandHandler, CallbackContext,
    MessageHandler, filters, ConversationHandler
)


def hoje_sp():
    return datetime.now(tz=ZoneInfo("America/Sao_Paulo")).date()


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

# Estados de conversa
(
    ADMIN_SENHA,
    ESPERANDO_SUPORTE,
    ADD_PONTOS_POR_ID,
    ADD_PONTOS_QTD,
    ADD_PONTOS_MOTIVO,
    DEL_PONTOS_ID,
    DEL_PONTOS_QTD,
    DEL_PONTOS_MOTIVO,
    ADD_ADMIN_ID,
    REM_ADMIN_ID,
    REMOVER_PONTUADOR_ID,
    BLOQUEAR_ID,
    BLOQUEAR_MOTIVO,
    DESBLOQUEAR_ID,
    ADD_PALAVRA_PROIBIDA,
    DEL_PALAVRA_PROIBIDA
) = range(16)

hoje = hoje_sp()

TEMPO_LIMITE_BUSCA = 10  # Tempo máximo (em segundos) para consulta


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        # Criação de tabelas se não existirem
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id            BIGINT PRIMARY KEY,
            username           TEXT NOT NULL DEFAULT 'vazio',
            first_name         TEXT NOT NULL DEFAULT 'vazio',
            last_name          TEXT NOT NULL DEFAULT 'vazio',
            pontos             INTEGER NOT NULL DEFAULT 0,
            nivel_atingido     INTEGER NOT NULL DEFAULT 0,
            is_pontuador       BOOLEAN NOT NULL DEFAULT FALSE,
            ultima_interacao   DATE,                                -- só para pontuar 1x/dia
            inserido_em        TIMESTAMP NOT NULL DEFAULT NOW(),    -- quando o usuário foi inserido
            atualizado_em      TIMESTAMP NOT NULL DEFAULT NOW(),     -- quando qualquer coluna for atualizada
            display_choice     VARCHAR(20) NOT NULL DEFAULT 'anonymous',
            nickname           VARCHAR(50)
        );

       CREATE TABLE IF NOT EXISTS historico_pontos (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES usuarios(user_id),
            pontos INTEGER NOT NULL,
            motivo TEXT NOT NULL DEFAULT 'Não Especificado',
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
       CREATE TABLE IF NOT EXISTS usuarios_bloqueados (
            user_id BIGINT PRIMARY KEY,
            motivo TEXT NOT NULL DEFAULT 'Não Especificado',
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

       CREATE TABLE IF NOT EXISTS palavras_proibidas (
            id SERIAL PRIMARY KEY,
            palavra TEXT UNIQUE NOT NULL
        );

       CREATE TABLE IF NOT EXISTS usuario_history (
            id           SERIAL    PRIMARY KEY,
            user_id      BIGINT    NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            status       TEXT      NOT NULL,         -- 'Inserido' ou 'Atualizado'
            username     TEXT      NOT NULL DEFAULT 'vazio',
            first_name   TEXT      NOT NULL DEFAULT 'vazio',
            last_name    TEXT      NOT NULL DEFAULT 'vazio',
            inserido_em  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            display_choice  VARCHAR(20) NOT NULL DEFAULT 'anonymous',
            nickname        VARCHAR(50)
        );
        """)


# --- Helpers de usuário (asyncpg) ---
PAGE_SIZE = 30
MAX_MESSAGE_LENGTH = 4000
HISTORICO_USER_ID = 4


async def adicionar_usuario_db(
        user_id: int,
        username: str = "vazio",
        first_name: str = "vazio",
        last_name: str = "vazio",
        display_choice: str = "anonymous",
        nickname: str | None = None,
        pool_override: asyncpg.Pool | None = None,
):
    pg = pool_override or pool
    async with pg.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                """
                SELECT username, first_name, last_name,
                       display_choice, nickname, ultima_interacao
                  FROM usuarios
                 WHERE user_id = $1::bigint
                """,
                user_id
            )

            if old:
                # Verifica o que mudou
                mudou_username = old['username'] != username
                mudou_firstname = old['first_name'] != first_name
                mudou_lastname = old['last_name'] != last_name
                mudou_display_choice = old['display_choice'] != display_choice
                mudou_nickname = old['nickname'] != nickname

                if mudou_username or mudou_firstname or mudou_lastname or mudou_display_choice or mudou_nickname:
                    logger.info(
                        f"[DB] {user_id} Atualizado: username: {username} "
                        f"firstname: {first_name} lastname: {last_name} "
                        f"displaychoice: {display_choice} nickname: {nickname}"
                    )
                    await conn.execute(
                        """
                        UPDATE usuarios
                           SET username      = $1,
                               first_name    = $2,
                               last_name     = $3,
                               display_choice     = $4,
                               nickname      = $5,
                               atualizado_em = NOW()
                         WHERE user_id      = $6::bigint
                        """,
                        username, first_name, last_name, display_choice, nickname, user_id
                    )

                    await conn.execute(
                        """
                        INSERT INTO usuario_history
                          (user_id, status, username, first_name, last_name, display_choice, nickname)
                        VALUES ($1::bigint, 'Atualizado', $2, $3, $4, $5, $6)
                        """,
                        user_id, username, first_name, last_name, display_choice, nickname
                    )

                    if old['ultima_interacao'] != hoje:
                        await conn.execute(
                            """
                            UPDATE usuarios
                               SET pontos = pontos + 1,
                                   ultima_interacao = $1
                             WHERE user_id = $2::bigint
                            """,
                            hoje, user_id
                        )
                        await conn.execute(
                            """
                            INSERT INTO historico_pontos
                              (user_id, pontos, motivo)
                            VALUES ($1::bigint, 1, 'ponto diário por atualização')
                            """,
                            user_id
                        )
            else:
                logger.info(
                    f"[DB] {user_id} Inserido: username: {username} "
                    f"firstname: {first_name} lastname: {last_name} "
                    f"display_choice: {display_choice} nickname: {nickname}"
                )
                await conn.execute(
                    """
                    INSERT INTO usuarios
                      (user_id, username, first_name, last_name,
                       display_choice, nickname,
                       inserido_em, ultima_interacao, pontos)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7, 1)
                    """,
                    user_id, username, first_name, last_name,
                    display_choice, nickname, hoje
                )

                await conn.execute(
                    """
                    INSERT INTO usuario_history
                      (user_id, status, username, first_name, last_name, display_choice, nickname)
                    VALUES ($1::bigint, 'Inserido', $2, $3, $4, $5, $6)
                    """,
                    user_id, username, first_name, last_name, display_choice, nickname
                )

                await conn.execute(
                    """
                    INSERT INTO historico_pontos (user_id, pontos, motivo)
                    VALUES ($1, 1, 'ponto diário por cadastro')
                    """,
                    user_id
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


async def bloquear_user_bd(user_id: int, motivo: str | None = None):
    """
    Bloqueia um usuário registrando o user_id e o motivo na tabela usuarios_bloqueados.
    Se o usuário já estiver bloqueado, atualiza motivo e timestamp.
    """
    await pool.execute(
        """
        INSERT INTO usuarios_bloqueados (user_id, motivo)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE
          SET motivo = EXCLUDED.motivo,
              data   = CURRENT_TIMESTAMP
        """,
        user_id,
        motivo
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
    # 1) Busca o usuário
    usuario = await obter_usuario_db(user_id)
    if not usuario:
        return None

    # 2) Calcula pontos atuais + delta
    pontos_atuais = usuario['pontos'] or 0
    novos = pontos_atuais + delta

    # 3) Determina status de pontuador e nível
    ja_pontuador = usuario['is_pontuador'] or False
    nivel = usuario['nivel_atingido'] or 0

    # 4) Registra no histórico
    await registrar_historico_db(user_id, delta, motivo)

    # 5) Verifica se virou pontuador pela primeira vez
    becomes_pontuador = False
    if not ja_pontuador and novos >= LIMIAR_PONTUADOR:
        ja_pontuador = True
        nivel += 1
        becomes_pontuador = True

    # 6) Verifica brinde de nível
    brinde = None
    for limiar, nome_brinde in NIVEIS_BRINDES.items():
        if pontos_atuais < limiar <= novos:
            brinde = nome_brinde
            nivel += 1
            break

    # 7) Atualiza no banco
    await pool.execute(
        """
        UPDATE usuarios
           SET pontos         = $1,
               is_pontuador   = $2,
               nivel_atingido = $3
         WHERE user_id = $4::bigint
        """,
        novos, ja_pontuador, nivel, user_id
    )

    # 8) Notificações ao admin, se houver bot
    if bot:
        texto_base = f"🔔 Usuário `{user_id}` atingiu {novos} pontos"
        if becomes_pontuador:
            texto = texto_base + " e virou *PONTUADOR*." + (f" Motivo: {motivo}" if motivo else "")
            asyncio.create_task(bot.send_message(chat_id=ID_ADMIN, text=texto, parse_mode='Markdown'))
        if brinde:
            texto = texto_base + f" e ganhou *{brinde}*." + (f" Motivo: {motivo}" if motivo else "")
            asyncio.create_task(bot.send_message(chat_id=ID_ADMIN, text=texto, parse_mode='Markdown'))

    # 9) Retorna o total atualizado
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
    "/historico_usuario – historico de nomes do usuário\n"
    "/add_admin – adicionar novo admin\n"
    "/rem_admin – remover admin\n"
    "/rem_pontuador – Remover permissão de pontuador\n"
    "/bloquear – Bloquear usuário\n"
    "/desbloquear – Desbloquear usuário\n"
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
async def bloquear_start(update: Update, context: CallbackContext):
    await update.message.reply_text("👤 Envie o ID do usuário que deseja bloquear.")
    return BLOQUEAR_ID


async def bloquear_usuario_id(update: Update, context: CallbackContext):
    try:
        context.user_data["bloquear_id"] = int(update.message.text)
        await update.message.reply_text("✍️ Envie o motivo do bloqueio.")
        return BLOQUEAR_MOTIVO
    except ValueError:
        return await update.message.reply_text("❌ ID inválido. Tente novamente.")


async def bloquear_usuario_motivo(update: Update, context: CallbackContext):
    motivo = update.message.text
    user_id = context.user_data["bloquear_id"]
    await bloquear_user_bd(user_id, motivo)
    await update.message.reply_text(f"✅ Usuário {user_id} bloqueado. Motivo: {motivo}")
    return ConversationHandler.END


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
    reg = await obter_bloqueado(user_id)  # agora await
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
    await bloquear_user_bd(alvo, motivo)  # await aqui
    await update.message.reply_text(f"✅ Usuário {alvo} bloqueado. Motivo: {motivo or 'nenhum'}")


async def desbloquear_usuario(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return await update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo = int(context.args[0])
    except:
        return await update.message.reply_text("Uso: /desbloquear <user_id>")
    await desbloquear_user_bd(alvo)  # await aqui
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
    await update.message.reply_text(
        "📝 Escreva sua mensagem de suporte (máx. 500 caracteres). Use /cancelar para abortar.")
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


ESCOLHENDO_DISPLAY, DIGITANDO_NICK = range(2)


# Handler para /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 1) Verifica se já existe registro; só insere uma vez
    perfil = await obter_usuario_db(user.id)
    if perfil is None:
        # insere usuário inicial com anonymous
        await adicionar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
            display_choice="anonymous",
            nickname=None,
        )

    # 2) Pergunta como ele quer aparecer
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Mostrar nome do jeito que está", callback_data="set:first_name")],
        [InlineKeyboardButton("2️⃣ Escolher um Nick/Apelido",       callback_data="set:nickname")],
        [InlineKeyboardButton("3️⃣ Ficar anônimo",                  callback_data="set:anonymous")],
    ])
    await update.message.reply_text(
        f"🤖 Bem-vindo, {user.first_name}! Para começar, caso você alcance o Ranking, como você gostaria de aparecer?",
        reply_markup=keyboard
    )
    return ESCOLHENDO_DISPLAY



async def tratar_display_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, escolha = query.data.split(":")
    user = query.from_user

    # 1️⃣ Se for “first_name”, grava e sai
    if escolha == "first_name":
        await adicionar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
            display_choice="first_name",
            nickname=None,
        )
        await query.edit_message_text("👍 Ok, você aparecerá com seu nome normal, para prosseguir escolha uma opção no menú ao lado.")
        return ConversationHandler.END

    # 2️⃣ Se for “nickname”, pede o nick e vai pro estado DIGITANDO_NICK
    if escolha == "nickname":
        await query.edit_message_text("✏️ Digite agora o nickname que você quer usar:")
        return DIGITANDO_NICK

    # 3️⃣ Se for “anonymous”, grava e sai
    if escolha == "anonymous":
        await adicionar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
            display_choice="anonymous",
            nickname=None,
        )
        await query.edit_message_text("✅ Preferência salva: Ficar anônimo, agora para prosseguir escolha uma opção no menu ao lado")
        return ConversationHandler.END

    # (Opcional) se vier qualquer outra callback_data
    return ConversationHandler.END


async def receber_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    nick = update.message.text.strip()

    await adicionar_usuario_db(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio",
        display_choice="nickname",
        nickname=nick,
    )
    await update.message.reply_text(f"✅ Nickname salvo: '' **{nick}** '', agora para prosseguir escolha uma opção no meu ao lado", parse_mode="Markdown")
    return ConversationHandler.END


async def meus_pontos(update: Update, context: CallbackContext):
    """
    Exibe ao usuário seus pontos e nível atual,
    tratando possíveis falhas de conexão ao banco.
    """
    user = update.effective_user

    try:
        # Tenta inserir ou atualizar o usuário
        await adicionar_usuario_db(
            user.id,
            user.username or "vazio",
            user.first_name or "vazio",
            user.last_name or "vazio",
            display_choice="anonymous",  # valor padrão aqui
            nickname=None  # ainda sem apelido
        )
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
    return ADD_PONTOS_QTD


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
    return ADD_PONTOS_MOTIVO


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
    pontos = context.user_data.pop("add_pt_value")
    motivo = context.user_data.pop("add_pt_reason")

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


async def tratar_presenca(update, context):
    user = update.effective_user

    # 1) Garante que exista sem logar toda vez
    await adicionar_usuario_db(user_id=user.id, username=user.username or "vazio",
                               first_name=user.first_name or "vazio", last_name=user.last_name or "vazio",
                               display_choice="anonymous", nickname=None)
    # 2) Busca registro completo
    reg = await obter_usuario_db(user.id)

    # 3) Dá pontuação uma única vez por dia,
    #    extraindo só a data do último timestamp
    ts = reg.get('ultima_interacao') if reg else None  # datetime.datetime ou None
    ultima_data = None if ts is None else ts.date() if hasattr(ts, 'date') else ts
    if ultima_data is None or ultima_data != hoje:
        # 3.1) Atribui o ponto
        await atualizar_pontos(user.id, 1, 'Presença diária', context.bot)

        # 3.2) Registra timestamp completo de agora
        from datetime import datetime
        from zoneinfo import ZoneInfo
        agora = datetime.now(tz=ZoneInfo("America/Sao_Paulo"))
        await pool.execute(
            "UPDATE usuarios SET ultima_interacao = $1 WHERE user_id = $2::bigint",
            agora, user.id
        )
        logger.info(f"[PRESENÇA] 1 ponto para {user.id} em {hoje}")


async def cancelar(update: Update, conText: ContextTypes.DEFAULT_TYPE):
    # Limpa tudo que já havia sido armazenado
    conText.user_data.clear()
    await update.message.reply_text(
        "❌ Operação cancelada. Nenhum dado foi salvo."
    )
    return ConversationHandler.END


# No topo do módulo, defina um separador para os logs agregados:
DELIM = '|'  # caractere que não aparece em usernames/nomes


async def historico_usuario(update: Update, context: CallbackContext):
    # 0) Autenticação de admin
    requester_id = update.effective_user.id
    if not context.user_data.get("is_admin"):
        await update.message.reply_text(
            "🔒 Você precisa autenticar: use /admin primeiro."
        )
        return ConversationHandler.END

    # 1) Detecta callback ou comando normal
    is_callback = getattr(update, "callback_query", None) is not None
    if not is_callback:
        await update.message.reply_text(
            "ℹ️ Se precisar de ajuda, digite /historico_usuario ajuda"
        )

    # 2) Texto de ajuda (exibido só em `/ajuda`)
    AJUDA_HISTORICO = (
        "*📘 Ajuda: /historico_usuario*\n\n"
        "Este comando retorna o histórico de alterações dos usuários.\n\n"
        "*Formas de uso:*\n"
        "`/historico_usuario` – Mostra os usuários sem filtro.\n"
        "`/historico_usuario <user_id>` – Mostra o histórico de um usuário.\n"
        "`/historico_usuario <user_id> <página>` – Página desejada.\n\n"
        "*Exemplos:*\n"
        "`/historico_usuario`\n"
        "`/historico_usuario 123456789`\n"
        "`/historico_usuario 123456789 2`\n\n"
        f"*ℹ️ Cada página exibe até {PAGE_SIZE} registros.*"
    )
    args = context.args or []
    if len(args) == 1 and args[0].lower() == "ajuda":
        await update.message.reply_text(
            AJUDA_HISTORICO, parse_mode="MarkdownV2"
        )
        return ConversationHandler.END

    # 3) Parsing de arguments: target_id e page
    target_id = None
    page = 1
    if len(args) == 1 and args[0].isdigit() and is_callback:
        page = int(args[0])
    elif len(args) == 1 and args[0].isdigit():
        target_id = int(args[0])
    elif len(args) == 2 and args[0].isdigit() and args[1].isdigit():
        target_id, page = int(args[0]), int(args[1])
    elif args:
        await update.message.reply_text(
            "❌ Uso incorreto. Digite `/historico_usuario ajuda`.",
            parse_mode="MarkdownV2"
        )
        return ConversationHandler.END

    offset = (page - 1) * PAGE_SIZE

    if target_id is None:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, inserido_em"
            " FROM usuario_history"
            " ORDER BY inserido_em DESC, id DESC"
            " LIMIT $1 OFFSET $2"
        )
        params = (PAGE_SIZE + 1, offset)
        header = f"🕒 Histórico completo (todos os usuários, página {page}):"
    else:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, inserido_em"
            " FROM usuario_history"
            " WHERE user_id = $1"
            " ORDER BY inserido_em DESC, id DESC"
            " LIMIT $2 OFFSET $3"
        )
        params = (target_id, PAGE_SIZE + 1, offset)
        header = (
            f"🕒 Histórico de alterações para `{target_id}` "
            f"(página {page}, {PAGE_SIZE} por página):"
        )

    # 5) Execução e slice (igual ao seu)
    rows = await pool.fetch(sql, *params)
    tem_mais = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    # 6) Sem registros
    if not rows:
        alvo = f" para `{target_id}`" if target_id else ""
        await update.message.reply_text(
            f"ℹ️ Sem histórico{alvo} na página {page}."
        )
        return

    # 7) Monta linhas
    lines = [escape_markdown_v2(header)]
    for r in rows:
        ts_str = r["inserido_em"].strftime("%d/%m %H:%M")
        prefix = r["status"]  # 'Inserido' ou 'Atualizado'
        user_part = f"`{r['user_id']}` " if target_id is None else ""
        lines.append(
            f"{ts_str} — {user_part}*{prefix}*: "
            f"username: `{escape_markdown_v2(r['username'])}` "
            f"firstname: `{escape_markdown_v2(r['first_name'])}` "
            f"lastname: `{escape_markdown_v2(r['last_name'])}`"
        )

    texto = "\n".join(lines)
    if len(texto) > MAX_MESSAGE_LENGTH:
        texto = texto[: MAX_MESSAGE_LENGTH - 50] + "\n\n⚠️ Texto truncado..."

    # 8) Botões de navegação
    botoes = []
    if page > 1:
        botoes.append(
            InlineKeyboardButton(
                "◀️ Anterior",
                callback_data=f"hist:{target_id or 0}:{page - 1}"
            )
        )
    if tem_mais:
        botoes.append(
            InlineKeyboardButton(
                "Próximo ▶️",
                callback_data=f"hist:{target_id or 0}:{page + 1}"
            )
        )
    markup = InlineKeyboardMarkup([botoes]) if botoes else None

    # 9) Envia a resposta
    await update.message.reply_text(
        texto,
        parse_mode="MarkdownV2",
        reply_markup=markup
    )

    # 10) Log de auditoria
    logger.info(
        "Admin %s consultou histórico %s (page %d)",
        requester_id,
        target_id or 'global',
        page
    )


async def callback_historico(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    try:
        prefixo, user_id_str, page_str = query.data.split(":")
        if prefixo != "hist":
            return
        target_id = int(user_id_str)
        page = int(page_str)
    except Exception:
        await query.edit_message_text("❌ Erro ao processar paginação.")
        return

    # Simula um update.message para reutilizar a lógica
    class FakeUpdate:
        def __init__(self, user, message, callback_query):
            self.effective_user = user
            self.message = message
            self.callback_query = callback_query

    # Verifica autenticação (usando o mesmo mét odo da função principal)
    requester_id = query.from_user.id
    if not context.user_data.get("is_admin"):
        await query.edit_message_text("🔒 Você precisa autenticar com /admin.")
        return

    # Rechama a função original reutilizando os parâmetros
    fake_update = FakeUpdate(query.from_user, query.message, query)
    context.args = [str(target_id)] if target_id != 0 else []
    context.args.append(str(page))
    await historico_usuario(fake_update, context)


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
        buttons.append(InlineKeyboardButton('◀️ Anterior', callback_data=f"usuarios|{page - 1}|{' '.join(args)}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton('Próxima ▶️', callback_data=f"usuarios|{page + 1}|{' '.join(args)}"))
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
        CommandHandler("admin", admin, filters=filters.ChatType.PRIVATE),
        CommandHandler("add_pontos", add_pontos),
        # CommandHandler("del_pontos", del_pontos),
        # CommandHandler("add_admin", add_admin),
        # CommandHandler("rem_admin", rem_admin),
        # CommandHandler("rem_pontuador", rem_pontuador),
        # CommandHandler("bloquear", bloquear),
        # CommandHandler("desbloquear", desbloquear),
        CommandHandler("add_palavra_proibida", add_palavra_proibida),
        CommandHandler("del_palavra_proibida", del_palavra_proibida),
    ],
    states={
        # /admin → senha
        ADMIN_SENHA: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_senha),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],
        # /add_pontos → id, qtd, motivo
        ADD_PONTOS_POR_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_IDuser),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],
        ADD_PONTOS_QTD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_quantidade),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],
        ADD_PONTOS_MOTIVO: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_motivo),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],

        # /del_pontos → id, qtd, motivo
        # DEL_PONTOS_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_IDuser),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # DEL_PONTOS_QTD: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_quantidade),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # DEL_PONTOS_MOTIVO: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_motivo),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],

        # # /add_admin → id
        # ADD_ADMIN_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_execute),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # # /rem_admin → id
        # REM_ADMIN_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, rem_admin_execute),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # # /rem_pontuador → id
        # REMOVER_PONTUADOR_ID: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, remover_pontuador),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],

        # /bloquear → id, motivo
        BLOQUEAR_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bloquear_usuario_id),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],
        BLOQUEAR_MOTIVO: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, bloquear_usuario_motivo),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],

        # /desbloquear → id
        DESBLOQUEAR_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, desbloquear_usuario),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        ],

        # # /add_palavra_proibida → palavra
        # ADD_PALAVRA_PROIBIDA: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_palavra_proibida_add),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
        # # /del_palavra_proibida → palavra
        # DEL_PALAVRA_PROIBIDA: [
        #     MessageHandler(filters.TEXT & ~filters.COMMAND, receber_palavra_proibida_del),
        #     MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar),
        # ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
    allow_reentry=True,
)


# --- Inicialização do bot ---
async def main():
    # 1) inicializa o pool ANTES de criar o Application
    await init_db_pool()
    # 2) agora monte o bot
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_description)
        .post_init(setup_commands)
        .build()
    )
    app.add_handler(main_conv)

    app.add_handler(
        ConversationHandler(
            entry_points = [CommandHandler("start", start, filters=filters.ChatType.PRIVATE)],
            states = {
                ESCOLHENDO_DISPLAY: [
                    CallbackQueryHandler(tratar_display_choice, pattern=r"^set:")
                ],
                DIGITANDO_NICK: [
                   MessageHandler(filters.TEXT & ~filters.COMMAND, receber_nickname),
                  MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancelar)
                ],
            },
            fallbacks = [CommandHandler("cancelar", cancelar)],
            allow_reentry = True,
        )
    )


    app.add_handler(CommandHandler('admin', admin, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('meus_pontos', meus_pontos))
    app.add_handler(CommandHandler('como_ganhar', como_ganhar))
    app.add_handler(CallbackQueryHandler(callback_historico, pattern=r"^hist:\d+:\d+$"))
    app.add_handler(CommandHandler('historico', historico))
    app.add_handler(CommandHandler('ranking_top10', ranking_top10))
    # app.add_handler(CommandHandler('ranking_top10q', ranking_top10q))
    app.add_handler(
        CommandHandler("historico_usuario", historico_usuario)
    )
    app.add_handler(
        CommandHandler("listar_usuarios", listar_usuarios)
    )
    app.add_handler(
        CommandHandler("total_usuarios", total_usuarios)
    )

    # Presença em grupos
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))
    app.add_handler(CallbackQueryHandler(callback_historico, pattern=r"^hist:\d+:\d+$"))

    logger.info("🔄 Iniciando polling...")
    await app.run_polling()


if __name__ == "__main__":

    try:
        nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())
    except Exception:
        logger.exception("❌ Erro durante run_polling")