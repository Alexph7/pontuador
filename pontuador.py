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
import io
import math
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, User
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler
from typing import Dict, Any
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, ContextTypes
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats
from telegram.ext import (
    CommandHandler, CallbackContext,
    MessageHandler, filters, ConversationHandler
)


def hoje_data_sp():
    return datetime.now(tz=ZoneInfo("America/Sao_Paulo")).date()


def hoje_hora_sp():
    return datetime.now(tz=ZoneInfo("America/Sao_Paulo"))


def format_dt_sp(dt: datetime | None, fmt: str = "%d/%m/%Y %H:%M:%S") -> str:
    """
    Converte um datetime (UTC ou outro fuso) para America/Sao_Paulo
    e retorna no formato especificado. Se dt for None, retorna "N/A".
    """
    if not dt:
        return "N/A"
    sp = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
    return sp.strftime(fmt)


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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

ADMIN_IDS = os.getenv("ADMIN_IDS", "")
if ADMIN_IDS:
    try:
        ADMINS = {int(x.strip()) for x in ADMIN_IDS.split(',') if x.strip()}
    except ValueError:
        logger.error("ADMIN_IDS deve conter apenas números separados por vírgula.")
        ADMINS = set()
else:
    ADMINS = set()

NIVEIS_BRINDES = {
    200: "🎁 Brinde nível 1",
    300: "🎁 Brinde nível 2",
    500: "🎁 Brinde nível 3",
    750: "🎁 Brinde nível 4",
    1000: "🎁 Brinde nível 5"
}

# Estados da conversa
(ADMIN_SENHA, ESPERANDO_SUPORTE, ADD_PONTOS_POR_ID, ADD_PONTOS_QTD, ADD_PONTOS_MOTIVO, DEL_PONTOS_ID, DEL_PONTOS_QTD,
 DEL_PONTOS_MOTIVO, ADD_ADMIN_ID, REM_ADMIN_ID) = range(10)

TEMPO_LIMITE_BUSCA = 10  # Tempo máximo (em segundos) para consulta

ranking_mensagens = {}


async def init_db_pool():
    global pool, ADMINS
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
            ultima_interacao   DATE,                                
            inserido_em        TIMESTAMPTZ NOT NULL DEFAULT NOW(),    -- quando o usuário foi inserido
            atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT NOW(),     -- quando qualquer coluna for atualizada
            display_choice     VARCHAR(20) NOT NULL DEFAULT 'indefinido',
            nickname           VARCHAR(50) NOT NULL DEFAULT 'sem nick',
            via_start          BOOLEAN NOT NULL DEFAULT FALSE
        );

       CREATE TABLE IF NOT EXISTS historico_pontos (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES usuarios(user_id),
            pontos INTEGER NOT NULL,
            motivo TEXT NOT NULL DEFAULT 'Não Especificado',
            data TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP
        );

       CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        );

       CREATE TABLE IF NOT EXISTS grupos_recomendacao (
           chat_id      BIGINT PRIMARY KEY,
           titulo       TEXT,
           registrado_em TIMESTAMP DEFAULT NOW()
        );

        -- 1) Guarda cada recomendação de lives (só o essencial)
       CREATE TABLE IF NOT EXISTS recomendacoes (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT      NOT NULL REFERENCES usuarios(user_id),
            nome_exibicao   TEXT        NOT NULL,
            link            TEXT        NOT NULL,
            moedas          SMALLINT    NOT NULL CHECK (moedas >= 5),
            criada_em       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        
        -- 2) Armazena os votos
       CREATE TABLE IF NOT EXISTS recomendacao_votos (
            rec_id      INTEGER     NOT NULL REFERENCES recomendacoes(id) ON DELETE CASCADE,
            voter_id    BIGINT      NOT NULL,                     -- quem votou
            voto        BOOLEAN     NOT NULL,                     -- TRUE = positivo, FALSE = negativo
            votado_em   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (rec_id, voter_id)
        );

       --- SQL para penalizações (execute uma vez no seu DB) ---
       CREATE TABLE IF NOT EXISTS penalizacoes (
         user_id     BIGINT PRIMARY KEY REFERENCES usuarios(user_id),
         strikes     INTEGER NOT NULL DEFAULT 0,
         bloqueado_ate TIMESTAMPTZ
        );
        
       CREATE TABLE IF NOT EXISTS ranking_recomendacoes (
            user_id BIGINT PRIMARY KEY,
            pontos  INTEGER NOT NULL DEFAULT 0
        );

       CREATE TABLE IF NOT EXISTS usuario_history (
            id           SERIAL    PRIMARY KEY,
            user_id      BIGINT    NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            status       TEXT      NOT NULL,         -- 'Inserido' ou 'Atualizado'
            username     TEXT      NOT NULL DEFAULT 'vazio',
            first_name   TEXT      NOT NULL DEFAULT 'vazio',
            last_name    TEXT      NOT NULL DEFAULT 'vazio',
            inserido_em  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            display_choice  VARCHAR(20) NOT NULL DEFAULT 'indefinido',
            nickname        VARCHAR(50) NOT NULL DEFAULT 'sem nick',
            via_start          BOOLEAN NOT NULL DEFAULT FALSE
        );
        """)
    ADMINS = await carregar_admins_db()


# --- Helpers de usuário (asyncpg) ---
PAGE_SIZE = 16
MAX_MESSAGE_LENGTH = 4000


async def adicionar_usuario_db(
        user_id: int,
        username: str = "vazio",
        first_name: str = "vazio",
        last_name: str = "vazio",
        display_choice: str = "indefinido",
        nickname: str = "sem nick",
        via_start: bool = False,
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
                        f"dischoice: {display_choice} nickname: {nickname}"
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

                    if old['ultima_interacao'] != hoje_data_sp():
                        await conn.execute(
                            """
                            UPDATE usuarios
                               SET pontos = pontos + 5,
                                   ultima_interacao = $1
                             WHERE user_id = $2::bigint
                            """,
                            hoje_data_sp(), user_id
                        )
                        await conn.execute(
                            """
                            INSERT INTO historico_pontos
                              (user_id, pontos, motivo)
                            VALUES ($1::bigint, 5, 'Check-in diário')
                            """,
                            user_id
                        )
            else:
                logger.info(
                    f"[DB] {user_id} Inserido: via_start={via_start}, username: {username} "
                    f"firstname: {first_name} lastname: {last_name} "
                    f"display_choice: {display_choice} nickname: {nickname}"
                )
                await conn.execute(
                    """
                    INSERT INTO usuarios
                      (user_id, username, first_name, last_name,
                       display_choice, nickname,
                       inserido_em, ultima_interacao, pontos, via_start)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7, 5, $8)
                    """,
                    user_id, username, first_name, last_name,
                    display_choice, nickname, hoje_data_sp(), via_start
                )

                await conn.execute(
                    """
                    INSERT INTO usuario_history
                      (user_id, status, username, first_name, last_name, display_choice, nickname, via_start)
                    VALUES ($1::bigint, 'Inserido', $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, username, first_name, last_name, display_choice, nickname, via_start
                )

                await conn.execute(
                    """
                    INSERT INTO historico_pontos (user_id, pontos, motivo)
                    VALUES ($1, 5, 'Check-in diário')
                    """,
                    user_id
                )


async def obter_ou_criar_usuario_db(
    user_id: int,
    username: str    = "vazio",
    first_name: str  = "vazio",
    last_name: str   = "vazio",
    via_start: bool  = False
):
    perfil = await pool.fetchrow("SELECT * FROM usuarios WHERE user_id = $1", user_id)

    if perfil:
        return perfil  # Já existe, retorna

    # Não existe: chama a função que já trata de inserir
    await adicionar_usuario_db(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        via_start=via_start
    )

    # Agora busca de novo e retorna
    perfil = await pool.fetchrow("SELECT * FROM usuarios WHERE user_id = $1", user_id)
    return perfil


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


def escape_markdown_v2(text: str) -> str:
    """
    Escapa caracteres reservados do MarkdownV2.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


async def verificar_canal(user_id: int, bot: Bot) -> tuple[bool, str]:
    try:
        membro = await bot.get_chat_member(chat_id="@cupomnavitrine", user_id=user_id)
        if membro.status not in ("member", "administrator", "creator"):
            return False, (
                "🚫 Para usar esse recurso, você precisa estar inscrito no canal @cupomnavitrine.\n"
                "👉 Acesse: https://t.me/cupomnavitrine"
            )
        return True, ""
    except:
        return False, (
            "🚫 Não foi possível verificar sua inscrição no canal.\n"
            "Tente novamente mais tarde."
        )

async def setup_commands(app):
    try:
        comandos_basicos = [
            BotCommand("inicio", "Volte ao começo"),
            BotCommand("meus_pontos", "Ver sua pontuação e nível"),
            BotCommand("rank_top10", "Top 10 de usuários por pts"),
            BotCommand("rank_lives", "Top 8 de usuários por pts de lives"),

        ]

        # 1) Comandos padrão (público)
        await app.bot.set_my_commands(
            comandos_basicos,
            scope=BotCommandScopeDefault()
        )

        # 2) Comandos em chat privado (com suporte)
        comandos_privados = comandos_basicos + [
            BotCommand("live", "Enviar Link de live com moedas"),
            BotCommand("historico", "Mostrar seu histórico de pontos"),
            BotCommand("como_ganhar", "Como ganhar mais pontos"),
            BotCommand("news", "Ver Novas Atualizações"),
        ]

        await app.bot.set_my_commands(
            comandos_privados,
            scope=BotCommandScopeAllPrivateChats()
        )

        logger.info("Comandos configurados para público e privado.")
    except Exception:
        logger.exception("Erro ao configurar comandos")


COMANDOS_PUBLICOS = [
    ("/meus_pontos", "Ver sua pontuação e nível"),
    ("/live", "Enviar link de live com moedas"),
    ("/historico", "Mostrar seu histórico de pontos"),
    ("/rank_top10", "Top 10 de usuários por pontos"),
    ("/rank_lives", "Top 8 de usuários por pontos de lives"),
    ("/como_ganhar", "Como ganhar mais pontos"),
    ("/news", "Ver Novas Atualizações"),
]

async def enviar_menu(chat_id: int, bot):
    texto_menu = "👋 Olá! Aqui estão os comandos que você pode usar:\n\n"
    for cmd, desc in COMANDOS_PUBLICOS:
        texto_menu += f"{cmd} — {desc}\n"
    await bot.send_message(chat_id=chat_id, text=texto_menu)

# mantém enviar_menu(chat_id: int, bot: Bot) do jeito que você já definiu

async def cmd_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # só repassa para enviar_menu
    await enviar_menu(update.effective_chat.id, context.bot)


ADMIN_MENU = (
    "🔧 *Menu Admin* 🔧\n\n"
    "/add - atribuir pontos a um usuário\n"
    "/del – remover pontos de um usuário\n"
    "/historico_usuario – historico de nomes do usuário\n"
    "/rem – remover admin\n"
    "/listar_usuarios – lista de usuarios cadastrados\n"
    "/estatisticas – quantidade total de usuarios cadastrados\n"
    "/listar_via_start – usuario que se cadastraram via start\n"
    "/registrar – Registrar grupo onde as mensagens de links de lives serao enviadas\n"
)


# Comando de admin
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in ADMINS:
        await update.message.reply_text(ADMIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("🔒 Digite a senha de admin:")
    return ADMIN_SENHA


async def tratar_senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    senha = update.message.text.strip()

    if senha == str(ADMIN_PASSWORD):
        await adicionar_admin_db(user_id)  # Salva no banco se quiser persistência
        ADMINS.add(user_id)  # Salva na memória enquanto o bot roda
        await update.message.reply_text(ADMIN_MENU)
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Senha incorreta. Tente novamente:")
        return ADMIN_SENHA


async def carregar_admins_db():
    try:
        registros = await pool.fetch("SELECT user_id FROM admins")
        return {r['user_id'] for r in registros}
    except Exception as e:
        logger.error(f"Erro ao carregar admins do banco: {e}")
        return set()


async def adicionar_admin_db(user_id: int):
    try:
        await pool.execute(
            "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id
        )
    except Exception as e:
        logger.error(f"Erro ao adicionar admin no banco: {e}")


async def remover_admin_db(user_id: int):
    try:
        await pool.execute(
            "DELETE FROM admins WHERE user_id = $1",
            user_id
        )
    except Exception as e:
        logger.error(f"Erro ao remover admin do banco: {e}")
        raise


ESCOLHENDO_DISPLAY, DIGITANDO_NICK = range(2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 🔒 Verifica se está no canal
    ok, msg = await verificar_canal(user.id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return ConversationHandler.END

    # 1) Verifica se já existe registro; só insere uma vez
    await obter_ou_criar_usuario_db(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio",
        via_start=True
    )

    # 2) Pergunta como ele quer aparecer
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Mostrar nome do jeito que está", callback_data="set:first_name")],
        [InlineKeyboardButton("2️⃣ Escolher um Nick/Apelido", callback_data="set:nickname")],
        [InlineKeyboardButton("3️⃣ Ficar anônimo", callback_data="set:anonymous")],
    ])
    await update.message.reply_text(
        f"🤖 Bem-vindo, {user.first_name}! Ao Prosseguir você aceita os termos de uso do bot \n"
        f" Para começar, caso você alcance o Ranking, como você gostaria de aparecer?",
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
            nickname="sem nick",
        )
        await query.edit_message_text(
            "👍 Ok, você aparecerá com seu nome normal, para prosseguir escolha uma opção no menu a seguir ou ao lado.")
        await enviar_menu(query.message.chat.id, context.bot)
        return ConversationHandler.END

    # 2️⃣ Se for “nickname”, pede o nick e vai pro estado DIGITANDO_NICK
    if escolha == "nickname":
        await query.edit_message_text("✏️ Digite agora o nickname que você quer usar:")
        return DIGITANDO_NICK

    # 3️⃣ Se for “anonymous”, gera inicial com fallback zero e salva
    if escolha == "anonymous":
        nome_base = (user.first_name or user.username or "0").strip()
        inicial = nome_base[0].upper() if nome_base else "0"
        anon = f"{inicial}****"

        await adicionar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
            display_choice="anonymous",
            nickname=anon,
        )

        await query.edit_message_text(
            f"✅ Você ficará anônimo como <code>{anon}</code>.\n\n"
            "Agora escolha uma opção no menu a seguir ou ao lado.",
            parse_mode=ParseMode.HTML
        )
        await enviar_menu(query.message.chat.id, context.bot)
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
    await update.message.reply_text(
        f"✅ Nickname salvo: '' **{nick}** '', agora para prosseguir escolha uma opção a seguir ou no menu ao lado",
        parse_mode="Markdown")
    await enviar_menu(update.effective_chat.id, context.bot)
    return ConversationHandler.END


async def perfil_invalido_ou_nao_inscrito(user_id: int, bot: Bot) -> tuple[bool, str]:
    # 1️⃣ Verifica se está no canal, usando o méto do já pronto
    ok, msg = await verificar_canal(user_id, bot)
    if not ok:
        return True, msg

    perfil = await pool.fetchrow(
        "SELECT display_choice, first_name, username, nickname FROM usuarios WHERE user_id = $1",
        user_id
    )

    if not perfil:
        return True, "⚠️ Você ainda não está cadastrado. Use /start para configurar seu perfil."

    if perfil["display_choice"] == "indefinido":
        return True, "⚠️ Seu perfil está incompleto. Use /start para configurá-lo corretamente."

    return False, ""  # está tudo OK


USUARIOS_POR_PAGINA = 20

async def listar_via_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    offset = (page - 1) * USUARIOS_POR_PAGINA

    try:
        total = await pool.fetchval("SELECT COUNT(*) FROM usuarios WHERE via_start = TRUE")
        usuarios = await pool.fetch(
            """
            SELECT user_id, username, first_name, last_name, inserido_em
              FROM usuarios
             WHERE via_start = TRUE
             ORDER BY inserido_em ASC
             LIMIT $1 OFFSET $2
            """,
            USUARIOS_POR_PAGINA, offset
        )

        if not usuarios:
            await update.message.reply_text("Nenhum usuário encontrado nesta página.")
            return

        linhas = []
        for u in usuarios:
            nome = u["first_name"] or ""
            sobrenome = u["last_name"] or ""
            username = f"@{u['username']}" if u["username"] != "vazio" else "nao tem"
            inserido_em = u["inserido_em"]
            data_registro = format_dt_sp(u["inserido_em"], "%d/%m/%Y %H:%M:%S")

            linhas.append(
                f"• Data: {data_registro} ID: `{u['user_id']}` Nome: {nome}  Sobrenome: {sobrenome} Username: {username}".strip()
            )
        texto = "*Usuários que entraram via /start:*\n\n" + "\n".join(linhas)
        texto += f"\n\nPágina {page} de {((total - 1) // USUARIOS_POR_PAGINA) + 1}"

        # Botões de paginação
        botoes = []
        if page > 1:
            botoes.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"via_start:{page - 1}"))
        if offset + USUARIOS_POR_PAGINA < total:
            botoes.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"via_start:{page + 1}"))

        markup = InlineKeyboardMarkup([botoes]) if botoes else None

        await update.message.reply_text(texto, parse_mode="MarkdownV2", reply_markup=markup)

    except Exception as e:
        logger.error(f"Erro ao listar via_start: {e}")
        await update.message.reply_text("❌ Ocorreu um erro ao listar os usuários.")


async def paginacao_via_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("via_start:"):
        return

    page = int(data.split(":")[1])
    context.args = [str(page)]
    update.message = query.message
    await listar_via_start(update, context)


async def meus_pontos(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = user.id
    username   = user.username or ""
    first_name = user.first_name or ""
    last_name  = user.last_name  or ""

    # Usa a função reutilizável para verificar se o usuário está no canal
    ok, msg = await verificar_canal(user_id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return

    try:
        # 1) Processa presença diária (vai dar 1 ponto se ainda não pontuou hoje)
        await processar_presenca_diaria(
            user_id,
            username,
            first_name or "",
            last_name or "",
            context.bot
        )

        # 2) Busca o perfil já com os pontos atualizados
        perfil = await pool.fetchrow(
            "SELECT pontos, nivel_atingido FROM usuarios WHERE user_id = $1",
            user_id
        )
        pontos = perfil['pontos']
        nivel = perfil['nivel_atingido']

        # Calcula a posição do usuário no ranking geral
        posicao = await pool.fetchval(
            "SELECT COUNT(*) + 1 FROM usuarios WHERE pontos > $1",
            pontos
        )
        if nivel == 0:
            nivel_texto = "rumo ao Nível 1"
        else:
            nivel_texto = f"Eba! Já alcançou brinde de Nível {nivel}"

        await update.message.reply_text(
            f"🎉 Você tem {pontos} pontos. {nivel_texto} 🏅 {posicao}º lugar."
        )

    except Exception as e:
        logger.error(f"Erro ao buscar pontos do usuário {user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Desculpe, tivemos um problema ao acessar as suas informações. "
            "Tente novamente mais tarde. Se o problema persistir, contate o suporte."
        )


async def como_ganhar(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    # Usa a função reutilizável para verificar se o usuário está no canal
    ok, msg = await verificar_canal(user_id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return

    # Ordena os brindes por nível de pontos
    brindes_texto = "\n".join(
        f"• {pontos} pontos – {descricao}"
        for pontos, descricao in sorted(NIVEIS_BRINDES.items())
    )
    await update.message.reply_text(
        "🎯*Pontos Válidos a Partir de 1 de Maio de 2025 a 30 de Junho*\n\n"
        "  *Você Pode Ganhar Pontos Por*:\n"
        "✅ Compras por ID em videos, ex: o produto do video custar $20\n"
        "mas com cupom e moedas o valor final for R$15, entao serão 15 pontos.\n\n"
        "✅ 05 pontos por comentar 1 vez em grupo ou interagir com o bot\n\n"
        "✅ Ganhe pontos indicando lives toque co comando /live. \n\n"
        "✅ 30 pontos por encontrar erros nos posts. \n\n"
        " Funciona assim: depois do post, se achar link que não funciona,\n"
        " link que leva a outro local, foto errada no post você ganha pontos.\n"
        "❌ Erros de ortografia não contam.\n"
        "❌ Também não vale se o erro foi da plataforma (ex: Mercado Livre, Shopee).\n\n"
        "💸 Como Você Pode Perder Pontos:\n"
        "❌ Trocas por brindes, desconta os pontos.\n"
        "❌ troca de ciclo ou fim do evento, os pontos zeram\n"
        "❌ Comportamento spamming, banimento\n"
        "❌ Produto devolvido (se aplicar)\n\n"
        f"{brindes_texto}\n\n",
        parse_mode="Markdown"
    )


async def news(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "🆕 *Novidades* (Junho 2025)\n\n"
        "Nova interação e ranking para lives, toque em /live e recomende um link \n\n"
        "no qual há live que irá sair moedas, no minimo 5\n\n"
        "você ganha pontos 10x o valor de moedas, exemplo: live com 5 moedas = 50 pontos\n\n"
        "os links serão enviados ao grupo e outros usuarios vão votar\n\n"
        "3 usuarios aleatorios poderao votar em positivo ou negativo 👍 ou 👎 \n"
        "conseguindo 2 votos os pontos serão adicionados, e votando em alguma recomendação vc ganha 10 pontos\n\n"
        "não conseguirá votar na própria recomendação, nem recomendar a mesma live duas vezes com mesmo link\n\n"
        "os melhores colocados no ranking ganham prêmio\n"
        "1ª lugar: R$80 em compras\n"
        "2ª lugar: R$50 em compras\n"
        "3ª lugar: R$30 em compras\n"
        "4ª ao 8ª lugar: R$19 em compras.\n\n"
        "Fora do bot, pode recomendar lives digitando o link e a quantidade de moedas\n"
        "exemplo:\n 'Vai sair 7 moedas na live -[LINK] ...' \n\n",
        parse_mode="Markdown"
    )


async def add_pontos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
        Inicia o fluxo de atribuição de pontos.
        Verifica se o usuário possui permissão temporária (senha válida) e
        pergunta qual é o user_id que receberá pontos.
        """
    requester_id = update.effective_user.id
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text(
            "🔒 Você precisa autenticar: use /admin primeiro."
        )
        return ConversationHandler.END

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
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("❗️ Valor inválido. Digite somente números positivos para os pontos.")

    qtd = int(text)

    if qtd <= 0:
        return await update.message.reply_text("❗️ O valor deve ser maior que zero.")

    context.user_data["add_pt_value"] = qtd
    await update.message.reply_text("📝 Por fim, qual o motivo para registrar no histórico?")
    return ADD_PONTOS_MOTIVO


async def add_pontos_motivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    motivo = update.message.text.strip()
    if not motivo:
        return await update.message.reply_text("❗️ Motivo não pode ficar em branco. Digite um texto.")
    context.user_data["add_pt_reason"] = motivo

    alvo_id = context.user_data.pop("add_pt_id")
    pontos = context.user_data.pop("add_pt_value")
    motivo = context.user_data.pop("add_pt_reason")

    novo_total = await atualizar_pontos(alvo_id, pontos, motivo, context.bot)
    await update.message.reply_text(
        f"✅ {pontos} pts atribuídos a {alvo_id}.\n"
        f"Motivo: {motivo}\n"
        f"Total agora: {novo_total} pts."
    )
    return ConversationHandler.END


async def del_pontos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Inicia o fluxo de remoção de pontos.
    """
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("🔒 Você precisa autenticar: use /admin primeiro.")
        return ConversationHandler.END

    await update.message.reply_text("🧾 Remoção de pontos: qual é o user_id do usuário?")
    return DEL_PONTOS_ID


async def del_pontos_IDuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("❗️ ID inválido. Digite apenas números.")

    context.user_data["del_pt_id"] = int(text)
    await update.message.reply_text("✏️ Quantos pontos deseja remover?")
    return DEL_PONTOS_QTD


async def del_pontos_quantidade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("❗️ Digite apenas números positivos.")

    qtd = int(text)
    if qtd <= 0:
        return await update.message.reply_text("❗️ O valor deve ser maior que zero.")

    context.user_data["del_pt_value"] = qtd
    await update.message.reply_text("📄 Qual o motivo dessa remoção?")
    return DEL_PONTOS_MOTIVO


async def del_pontos_motivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    motivo = update.message.text.strip()
    if not motivo:
        return await update.message.reply_text("❗️ Motivo obrigatório. Digite um texto.")

    alvo_id = context.user_data.pop("del_pt_id")
    pontos = context.user_data.pop("del_pt_value")
    motivo = update.message.text.strip()

    novo_total = await atualizar_pontos(alvo_id, -pontos, f"(removido) {motivo}", context.bot)

    await update.message.reply_text(
        f"✅ {pontos} pts removidos de {alvo_id}.\n"
        f"Motivo: {motivo}\n"
        f"Total agora: {novo_total} pts."
    )
    return ConversationHandler.END


async def atualizar_pontos(
        user_id: int,
        delta: int,
        motivo: str = None,
        bot: Bot = None
) -> int | None:
    try:
        chat = await bot.get_chat(user_id)
        username = chat.username or "vazio"
        first_name = chat.first_name or "vazio"
        last_name = chat.last_name or "vazio"
    except Exception:
        username = "vazio"
        first_name = "vazio"
        last_name = "vazio"

    usuario = await obter_ou_criar_usuario_db(
        user_id, username, first_name, last_name
    )
    if not usuario:
        return None

    pontos_atuais = usuario['pontos'] or 0
    novos = pontos_atuais + delta
    nivel = 0

    await registrar_historico_db(user_id, delta, motivo)

    nivel = sum(1 for limiar in NIVEIS_BRINDES if novos >= limiar)

    await pool.execute(
        """
        UPDATE usuarios
           SET pontos = $1,
               nivel_atingido = $2
         WHERE user_id = $3::bigint
        """,
        novos, nivel, user_id
    )
    return novos


async def historico(update: Update, context: CallbackContext):
    user = update.effective_user
    rows = await pool.fetch(
        """
        SELECT data, pontos, motivo
          FROM historico_pontos
         WHERE user_id = $1
      ORDER BY data DESC
         LIMIT 50
        """,
        user.id
    )
    if not rows:
        await update.message.reply_text("🗒️ Nenhum registro de histórico encontrado.")
        return
    lines = [
        f" {format_dt_sp(r['data'], '%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}"
        for r in rows
    ]
    await update.message.reply_text("🗒️ Seu histórico de pontos:\n\n" + "\n\n".join(lines))


async def ranking_top10(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id    = user.id
    username   = user.username   or ""
    first_name = user.first_name or ""
    last_name  = user.last_name  or ""
    chat_id    = update.effective_chat.id

    # 1) Presença diária unificada — agora com 5 args:
    await processar_presenca_diaria(
        user_id,
        username,
        first_name,
        last_name,
        context.bot
    )

    if update.effective_chat.type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    mensagem_antiga_id = ranking_mensagens.get(chat_id)
    if mensagem_antiga_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mensagem_antiga_id)
        except:
            pass  # Ignora erro se a mensagem já tiver sido apagada manualmente

    # Busca top 10
    top = await pool.fetch(
        """
        SELECT
            user_id,
            username,
            first_name,
            display_choice,
            nickname,
            pontos
        FROM usuarios
        ORDER BY pontos DESC
        LIMIT 10
        """
    )

    if not top:
        msg = await update.message.reply_text("🏅 Nenhum usuário cadastrado no ranking.")
        ranking_mensagens[chat_id] = msg.message_id
        return

    linhas = ["🏅 Top 10 de pontos:"]
    for i, u in enumerate(top):
        choice = u["display_choice"]
        if choice == "first_name":
            display = u["first_name"] or u["username"] or "Usuário"
        elif choice in ("nickname", "anonymous"):
            display = u["nickname"] or u["username"] or "Usuário"
        elif choice == "indefinido":
            display = "Esperando interação"
        else:
            display = u["username"] or u["first_name"] or "Usuário"

        linhas.append(f"{i + 1}. {display} - {u['pontos']} pts")

    texto = "\n".join(linhas)
    msg = await update.message.reply_text(texto)

    ranking_mensagens[chat_id] = msg.message_id


async def tratar_presenca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.is_bot:
        return

    await processar_presenca_diaria(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio",
        bot=context.bot
    )

async def processar_presenca_diaria(
    user_id: int,
    username: str,
    first_name: str,
    last_name: str,
    bot: Bot
) -> int | None:
    perfil = await obter_ou_criar_usuario_db(
        user_id=user_id,
        username=username or "vazio",
        first_name=first_name or "vazio",
        last_name=last_name or "vazio",
        via_start=False  # Porque foi no grupo
    )
    # Se ainda não pontuou hoje…
    if perfil["ultima_interacao"] != hoje_data_sp():
        # Dá 1 ponto e atualiza última interação
        novo_total = await atualizar_pontos(user_id, 3, "Presença diária", bot)
        await pool.execute(
            "UPDATE usuarios SET ultima_interacao = $1 WHERE user_id = $2::bigint",
            hoje_data_sp(), user_id
        )
        return novo_total
    return None


async def cancel(update: Update, conText: ContextTypes.DEFAULT_TYPE):
    # Limpa tudo que já havia sido armazenado
    conText.user_data.clear()
    await update.message.reply_text(
        "❌ Operação cancelada. Nenhum dado foi salvo."
    )
    return ConversationHandler.END
    return ConversationHandler.END


async def historico_usuario(update: Update, context: CallbackContext):
    # 0) Autenticação de admin
    requester_id = update.effective_user.id

    if requester_id not in ADMINS:
        await update.message.reply_text("🔒 Você precisa autenticar: use /admin primeiro.")
        return ConversationHandler.END

    # 1) Detecta callback ou comando normal
    is_callback = getattr(update, "callback_query", None) is not None
    if not is_callback:
        await update.message.reply_text(
            "ℹ️ Precisar de ajuda digite `/historico_usuario ajuda`",
            parse_mode="MarkdownV2"
        )

    AJUDA_HISTORICO = (
        "*📘 Ajuda com parâmetros*\n\n"
        "Este comando retorna o histórico de alterações dos usuários\n\n"
        "*Formas de uso:*\n"
        "`/historico\\_usuario` – Mostra todo os usuários sem filtro\n"
        "`/historico\\_usuario <user_id>` – Mostra o histórico de um usuário\n"
        "`/historico\\_usuario <user_id> <página>` – Mostra o histórico de um usuário em pagina desejada\n"
        "`/historico\\_usuario <nickname> <página>` – Mostra o histórico de um usuário pelo nickname e página\n\n"
        "*Exemplos:*\n"
        "`/historico\\_usuario`\n"
        "`/historico\\_usuario 123456789`\n"
        "`/historico\\_usuario 123456789 2`\n"
        "`/historico\\_usuario joaosilva 2`\n\n"
        f"*ℹ️ Cada página exibe até {PAGE_SIZE} registros*"
    )

    args = context.args or []
    if len(args) == 1 and args[0].lower() == "ajuda":
        await update.message.reply_text(
            AJUDA_HISTORICO, parse_mode="MarkdownV2"
        )
        return ConversationHandler.END

    # 3) Parsing de arguments: target_id e page
    target_id = None
    nickname = None
    page = 1
    if len(args) == 1 and args[0].isdigit() and is_callback:
        page = int(args[0])
    elif len(args) == 1 and args[0].isdigit():
        target_id = int(args[0])
    elif len(args) == 2 and args[0].isdigit() and args[1].isdigit():
        target_id, page = int(args[0]), int(args[1])
    elif len(args) == 1:
        nickname = args[0]
    elif len(args) == 2 and args[1].isdigit():
        nickname = args[0]
        page = int(args[1])
    elif args:
        await update.message.reply_text(
            "Uso incorreto Digite `/historico_usuario ajuda`",
            parse_mode="MarkdownV2"
        )
        return ConversationHandler.END

    offset = (page - 1) * PAGE_SIZE
    if nickname:
        sql_nick = (
            "SELECT user_id FROM usuario_history "
            "WHERE nickname = $1 "
            "ORDER BY inserido_em DESC LIMIT 1"
        )
        row = await pool.fetchrow(sql_nick, nickname)
        if row:
            target_id = row["user_id"]
        else:
            await update.message.reply_text(
                f"⚠️ Nickname `{escape_markdown_v2(nickname)}` não encontrado no histórico",
                parse_mode="MarkdownV2"
            )
            return ConversationHandler.END

    # 4) Executa a query (sem definir header aqui)
    if target_id is None:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, display_choice, nickname, inserido_em "
            "FROM usuario_history "
            "ORDER BY inserido_em DESC, id DESC "
            "LIMIT $1 OFFSET $2"
        )
        params = (PAGE_SIZE + 1, offset)
    else:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, display_choice, nickname, inserido_em "
            "FROM usuario_history "
            "WHERE user_id = $1 "
            "ORDER BY inserido_em DESC, id DESC "
            "LIMIT $2 OFFSET $3"
        )
        params = (target_id, PAGE_SIZE + 1, offset)

    rows = await pool.fetch(sql, *params)
    tem_mais = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    # 5) Se não há registros
    if not rows:
        if target_id is None:
            await update.message.reply_text(
                f"ℹ️ Sem histórico na página {page}",
                parse_mode="MarkdownV2"
            )
        else:
            alvo = nickname or str(target_id)
            alvo_esc = escape_markdown_v2(str(target_id))
            await update.message.reply_text(
                f"ℹ️ Sem histórico para `{alvo_esc}` na página {page}",
                parse_mode="MarkdownV2"
            )
        return

    # 6) Monta o header definitivo apenas aqui (sem duplicação)
    if target_id is None:
        header = f"🕒 Histórico completo \\(todos os usuários, página {page}\\):"
    else:
        user_id_escapado = escape_markdown_v2(str(target_id))
        header = (
            f"🕒 Histórico de alterações para `{user_id_escapado}` "
            f"\\(página {page}, {PAGE_SIZE} por página\\):"
        )

    lines = [header]
    for r in rows:
        ts_str = format_dt_sp(r["inserido_em"], "%d/%m %H:%M")
        prefix = r["status"]  # 'Inserido' ou 'Atualizado'
        user_part = f"`{r['user_id']}` " if target_id is None else ""
        lines.append(
            f"{ts_str} — {user_part}*{prefix}*: "
            f"username: `{escape_markdown_v2(r['username'])}` "
            f"firstname: `{escape_markdown_v2(r['first_name'])}` "
            f"lastname: `{escape_markdown_v2(r['last_name'])}` "
            f"dischoice: `{escape_markdown_v2(r['display_choice'])}` "
            f"nickname: `{escape_markdown_v2(r['nickname'])}`"
        )

    # 8) Truncamento linha a linha (nunca cortando no meio de uma formatação)
    final_lines = []
    total_length = 0

    for line in lines:
        # +1 para contabilizar o '\n' que será inserido
        if total_length + len(line) + 1 > MAX_MESSAGE_LENGTH - 50:
            final_lines.append("⚠️ Atenção: parte da mensagem omitida por exceder o limite")
            break
        final_lines.append(line)
        total_length += len(line) + 1

    texto = "\n".join(final_lines)

    # 9) Botões de navegação
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
    try:
        await update.message.reply_text(
            texto,
            parse_mode="MarkdownV2",
            reply_markup=markup
        )
    except BadRequest as err:
        # 1) Extrair o “byte offset” da mensagem de erro
        #    Normalmente a mensagem do err tem algo como:
        #    "Can't parse entities: can't find end of italic entity at byte offset 2529"
        msg = str(err)
        match = re.search(r'byte offset (\d+)', msg)
        if match:
            offset = int(match.group(1))
        else:
            offset = None

        # 2) Se achamos o offset, imprimimos um trecho antes e depois dele
        if offset is not None:
            start = max(0, offset - 100)
            end = min(len(texto), offset + 100)
            trecho_com_erro = texto[start:end]

            # Imprime no console/log para você ver exatamente onde está o problema
            logger.error("MarkdownV2 inválido em byte offset %d", offset)
            logger.error("Trecho ao redor do offset:\n>>> %r <<<", trecho_com_erro)

            # Se quiser também mandar para o próprio chat (apenas para DEBUG):
            await update.message.reply_text(
                f"⚠️ Erro de formatação em byte offset {offset}.\n"
                f"Trecho com problema:\n<pre>{trecho_com_erro}</pre>",
                parse_mode="HTML"
            )
        else:
            # Se não encontramos o offset, mostramos a mensagem inteira de erro:
            logger.error("BadRequest sem offset detectado: %s", msg)
            await update.message.reply_text(
                f"⚠️ Erro inesperado de MarkdownV2:\n<pre>{msg}</pre>",
                parse_mode="HTML"
            )

        # Opcional: re-raise para interromper (ou só sair do handler)
        return

    # 11) Log de auditoria
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
        await query.edit_message_text("❌ Erro ao processar paginação")
        return

    # Simula um update.message para reutilizar a lógica
    class FakeUpdate:
        def __init__(self, user, message, callback_query):
            self.effective_user = user
            self.message = message
            self.callback_query = callback_query

    # Rechama a função original reutilizando os parâmetros
    fake_update = FakeUpdate(query.from_user, query.message, query)
    context.args = [str(target_id)] if target_id != 0 else []
    context.args.append(str(page))
    await historico_usuario(fake_update, context)


async def rem_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) Buscar lista de admins no banco
    rows = await pool.fetch("SELECT user_id FROM admins ORDER BY user_id")
    admin_ids = [row["user_id"] for row in rows]

    if not admin_ids:
        return await update.message.reply_text("⚠️ Não há administradores registrados.")

    # 2) Montar texto enumerado e salvar em context.user_data
    texto_listagem = "👥 Lista de Admins:\n\n"
    for i, uid in enumerate(admin_ids, start=1):
        texto_listagem += f"{i}. <code>{uid}</code>\n"
    texto_listagem += "\nDigite o número correspondente ao admin que deseja remover:"

    context.user_data["admin_lista"] = admin_ids
    await update.message.reply_text(texto_listagem, parse_mode="HTML")

    # Retorna o estado onde o próximo handler será chamado
    return REM_ADMIN_ID


async def rem_admin_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()

    # 3) Validação básica
    if not texto.isdigit():
        return await update.message.reply_text("❗️Digite apenas o número correspondente.")

    indice = int(texto) - 1
    lista = context.user_data.get("admin_lista", [])

    if indice < 0 or indice >= len(lista):
        return await update.message.reply_text("❗️Número inválido.")

    alvo_id = lista[indice]

    # 4) Remover do banco
    await pool.execute("DELETE FROM admins WHERE user_id = $1", alvo_id)

    # 5) Remover do set local (se existir)
    ADMINS.discard(alvo_id)

    await update.message.reply_text(
        f"✅ Admin removido com sucesso: <code>{alvo_id}</code>",
        parse_mode="HTML"
    )

    # Ótimo prática: limpar user_data para não deixar lixo
    del context.user_data["admin_lista"]
    return ConversationHandler.END


PAGE_SIZE_LISTAR = 50


async def listar_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lista usuários cadastrados de forma paginada, exibindo índice geral (1-based), user_id e first_name
    (ou "username: <valor>" se first_name estiver vazio, ou "vazio" se ambos estiverem vazios).

    Uso: /listar_usuarios [<número_da_página>]
    Exemplo:
      /listar_usuarios        → página 1
      /listar_usuarios 2      → página 2
    """
    # 1) Determinar qual página está sendo solicitada (default = 1)
    args = context.args or []
    try:
        page = int(args[0]) if args else 1
    except ValueError:
        await update.message.reply_text("❌ Página inválida. Use /listar_usuarios <número>.")
        return

    if page < 1:
        page = 1

    # 2) Calcular total de usuários para saber quantas páginas existem
    try:
        total_usuarios = await pool.fetchval("SELECT COUNT(*) FROM usuarios")
    except Exception as e:
        logger.error(f"Erro ao contar usuários: {e}")
        await update.message.reply_text("❌ Não foi possível obter o total de usuários.")
        return

    total_paginas = max(1, math.ceil(total_usuarios / PAGE_SIZE_LISTAR))
    if page > total_paginas:
        await update.message.reply_text(
            f"ℹ️ A página {page} não existe. Só há {total_paginas} páginas"
        )
        return

    # 3) Buscar só os usuários daquela página
    offset = (page - 1) * PAGE_SIZE_LISTAR
    try:
        rows = await pool.fetch(
            "SELECT user_id, first_name, username FROM usuarios ORDER BY user_id LIMIT $1 OFFSET $2",
            PAGE_SIZE_LISTAR,
            offset
        )
    except Exception as e:
        logger.error(f"Erro ao buscar usuários: {e}")
        await update.message.reply_text("❌ Não foi possível acessar a lista de usuários.")
        return

    if not rows:
        await update.message.reply_text("ℹ️ Nenhum usuário encontrado nesta página.")
        return

    # 4) Montar as linhas da mensagem
    lines = []
    # Para numerar corretamente de 1 até total_usuarios, calculamos o índice global:
    # índice_global = offset + índice_na_página (1-based)
    for i, row in enumerate(rows, start=1):
        indice_global = offset + i
        user_id = row["user_id"]
        first_name_raw = (row["first_name"] or "").strip()
        username_raw = (row["username"] or "").strip()

        if first_name_raw:
            display = first_name_raw
        elif username_raw:
            display = f"username: {username_raw}"
        else:
            display = "vazio"

        # Escapa caracteres especiais para MarkdownV2
        display_esc = escape_markdown_v2(display)

        # Escapamos o ponto após o índice (ex: “1\.”) para o MarkdownV2 aceitar
        lines.append(f"{indice_global}\\.`{user_id}` — {display_esc}")

    # 5) Texto final
    header = f"👥 **Usuários cadastrados \\(página {page}/{total_paginas}, total {total_usuarios}\\):**\n\n"
    texto = header + "\n".join(lines)

    # 6) Botões de navegação (Anterior / Próximo) se houver mais de uma página
    buttons = []
    if page > 1:
        buttons.append(
            InlineKeyboardButton("◀️ Anterior", callback_data=f"usuarios|{page - 1}")
        )
    if page < total_paginas:
        buttons.append(
            InlineKeyboardButton("Próximo ▶️", callback_data=f"usuarios|{page + 1}")
        )
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None

    # 7) Enviar (ou editar mensagem se for callback)
    if update.callback_query:
        # Se veio de um callback inline, editamos a mensagem existente
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            texto,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
    else:
        # Se veio de um comando /listar_usuarios
        await update.message.reply_text(
            texto,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )


# Callback para navegação de páginas
async def callback_listar_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # O callback_data foi definido como "usuarios|<pagina>"
    data = query.data.split("|")
    if data[0] != "usuarios":
        return  # não é o callback esperado
    try:
        nova_pagina = int(data[1])
    except (IndexError, ValueError):
        return

    # Simula args e chama listar_usuarios novamente, agora em modo callback
    context.args = [str(nova_pagina)]
    # Reaproveita a mesma função para editar a mensagem
    await listar_usuarios(update, context)


from telegram import Update
from telegram.ext import ContextTypes


async def estatisticas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando que exibe estatísticas agregadas “no tempo t odo” e “hoje”:

    ➤ No tempo t odo (All time):
      1. Total de usuários cadastrados
      2. Usuários que já interagiram (receberam pelo menos 1 ponto)
      3. Usuários que já atingiram algum nível (nivel_atingido > 0)
      4. Total de pontos distribuídos
      5. Total de pontos removidos

    ➤ Hoje:
      1. Novos usuários cadastrados hoje
      2. Usuários que interagiram (pontuados) hoje
      3. Usuários que atingiram nível hoje
      4. Total de pontos distribuídos hoje
      5. Total de pontos removidos hoje
    """
    try:
        hoje = hoje_data_sp()  # data de hoje em America/Sao_Paulo

        # === NO TEMPO T'ODO ===

        # Total de usuários cadastrados (all time)
        total_usuarios = await pool.fetchval(
            "SELECT COUNT(*) FROM usuarios"
        )

        # Usuários que já atingiram algum nível (nivel_atingido > 0)
        usuarios_nivel_total = await pool.fetchval(
            "SELECT COUNT(*) FROM usuarios WHERE nivel_atingido > 0"
        )

        # Total de pontos distribuídos (soma de pontos positivos, all time)
        pontos_distribuidos_total = await pool.fetchval(
            "SELECT COALESCE(SUM(pontos), 0) FROM historico_pontos WHERE pontos > 0"
        )

        # 5) Total de pontos removidos (soma absoluta de pontos negativos, all time)
        pontos_removidos_total = await pool.fetchval(
            "SELECT COALESCE(SUM(ABS(pontos)), 0) FROM historico_pontos WHERE pontos < 0"
        )

        # === HOJE ===

        # Novos usuários cadastrados hoje (DATE(inserido_em) = hoje)
        inseridos_hoje = await pool.fetchval(
            "SELECT COUNT(*) FROM usuarios WHERE DATE(inserido_em) = $1",
            hoje
        )

        # Usuários que interagiram (pontuados) hoje
        interagiram_hoje = await pool.fetchval(
            """
            SELECT COUNT(DISTINCT user_id)
              FROM historico_pontos
             WHERE pontos > 0
               AND DATE(data) = $1
            """,
            hoje
        )

        # Usuários que atingiram nível hoje
        #   -> Para cada user_id com soma positiva hoje, checar se cruzou um limiar de NIVEIS_BRINDES
        rows_dia = await pool.fetch(
            """
            SELECT user_id, SUM(pontos) AS soma_dia
              FROM historico_pontos
             WHERE pontos > 0
               AND DATE(data) = $1
          GROUP BY user_id
            """,
            hoje
        )

        niveis = sorted(NIVEIS_BRINDES.keys())  # [200, 300, 500, 750, 1000]
        usuarios_nivel_hoje = 0

        for rec in rows_dia:
            uid = rec["user_id"]
            soma_dia = rec["soma_dia"] or 0

            soma_antes = await pool.fetchval(
                """
                SELECT COALESCE(SUM(pontos), 0)
                  FROM historico_pontos
                 WHERE user_id = $1
                   AND DATE(data) < $2
                """,
                uid,
                hoje
            )

            total_hoje = soma_antes + soma_dia

            # Verifica se cruzou algum limiar hoje
            cruzou = any(soma_antes < limiar <= total_hoje for limiar in niveis)
            if cruzou:
                usuarios_nivel_hoje += 1

        # Total de pontos distribuídos hoje (soma de pontos positivos)
        pontos_distribuidos_hoje = await pool.fetchval(
            """
            SELECT COALESCE(SUM(pontos), 0)
              FROM historico_pontos
             WHERE pontos > 0
               AND DATE(data) = $1
            """,
            hoje
        )

        # Total de pontos removidos hoje (soma absoluta de pontos negativos)
        pontos_removidos_hoje = await pool.fetchval(
            """
            SELECT COALESCE(SUM(ABS(pontos)), 0)
              FROM historico_pontos
             WHERE pontos < 0
               AND DATE(data) = $1
            """,
            hoje
        )

        # Monta mensagem final
        texto = (
            "📊 *Estatísticas de Usuários*\n\n"
            "*No tempo todo:*\n"
            f"• Total de usuários cadastrados: *{total_usuarios}*\n"
            f"• Usuários que já atingiram nível: *{usuarios_nivel_total}*\n"
            f"• Total de pontos distribuídos: *{pontos_distribuidos_total}*\n"
            f"• Total de pontos removidos: *{pontos_removidos_total}*\n\n"
            "*Hoje \\({hoje_str}\\):*\n"
            f"• Novos usuários cadastrados: *{inseridos_hoje}*\n"
            f"• Usuários que interagiram hoje: *{interagiram_hoje}*\n"
            f"• Usuários que atingiram nível hoje: *{usuarios_nivel_hoje}*\n"
            f"• Pontos distribuídos hoje: *{pontos_distribuidos_hoje}*\n"
            f"• Pontos removidos hoje: *{pontos_removidos_hoje}*"
        ).replace("{hoje_str}", hoje.strftime("%d/%m/%Y"))

        await update.message.reply_text(texto, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"Erro ao gerar estatísticas: {e}")
        await update.message.reply_text("❌ Não foi possível gerar as estatísticas no momento")


LIVE_LINK, LIVE_MOEDAS = range(2)

async def registrar_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMINS:
        return

    # continua usando chat como antes
    chat = update.effective_chat

    if chat.type in ("group", "supergroup"):
        await pool.execute(
            """
            INSERT INTO grupos_recomendacao (chat_id, titulo)
            VALUES ($1, $2)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            chat.id, chat.title or ""
        )
        await update.message.reply_text("✅ Grupo registrado para receber recomendações!")
    else:
        await update.message.reply_text("❌ Este comando só pode ser usado em grupos.")


# 1️⃣ Handler do comando /live
async def live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    ok, msg = await verificar_canal(user_id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return ConversationHandler.END  # ou return -1, depende do seu fluxo

    await update.message.reply_text(
        "📎 Por favor, envie o link da live.\n"
        "Deve começar com `br.shp.`, É melhor sugerir lives que estão prestes a liberar moedas, para dar tempo."
    )
    return LIVE_LINK


# 2️⃣ Recebe e valida o link
async def live_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    pattern = re.compile(r'^(?:https?://)?br\.shp\.ee(?:/.*)?$')

    if not pattern.match(link) or len(link) > 28:
        await update.message.reply_text(
            "❌ Link inválido. Ele deve começar com `br.shp.ee` ou nao é válido. \nTente novamente:"
        )
        return LIVE_LINK

    user_id = update.effective_user.id
    existe = await pool.fetchval(
        "SELECT 1 FROM recomendacoes WHERE user_id = $1 AND link = $2",
        user_id, link
    )
    if existe:
        await update.message.reply_text(
            "⚠️ Você já recomendou esse link antes. Envie outro link:"
        )
        return LIVE_LINK

    context.user_data["link"] = link
    await update.message.reply_text("💰 Agora, quantas moedas essa live vale? (mínimo 5)")
    return LIVE_MOEDAS

# 3️⃣ Recebe e valida as moedas
async def live_receive_moedas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 5:
        await update.message.reply_text("❌ Valor inválido. Envie inteiro ≥ 5:")
        return LIVE_MOEDAS

    moedas = int(text)
    user = update.effective_user
    link = context.user_data["link"]

    perfil = await pool.fetchrow(
        "SELECT display_choice, first_name, username, nickname FROM usuarios WHERE user_id=$1",
        user.id
    )
    dc = perfil["display_choice"]
    nome = (
        perfil["first_name"] if dc == "first_name" else
        perfil["nickname"] if dc == "nickname" else
        perfil["username"] or "Usuário"
    )

    rec = await pool.fetchrow(
        "INSERT INTO recomendacoes (user_id, nome_exibicao, link, moedas) "
        "VALUES ($1,$2,$3,$4) RETURNING id",
        user.id, nome, link, moedas
    )
    rec_id = rec["id"]

    texto = (
        f"📣 *{nome}* recomendou uma live com *{moedas} moedas!*\n"
        f"🔗 {link}\n\n"
        "Esta recomendação é verdadeira? Vote e ganhe pontos também!"
    )
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("👍", callback_data=f"voto:{rec_id}:1"),
        InlineKeyboardButton("👎", callback_data=f"voto:{rec_id}:0"),
    ]])

    grupos = await pool.fetch("SELECT chat_id FROM grupos_recomendacao")
    for row in grupos:
        try:
            msg = await context.bot.send_message(
                chat_id=row["chat_id"],
                text=texto,
                reply_markup=teclado,
                parse_mode="Markdown"
            )

            # agenda exclusão da recomendação após 20 minutos (1200s)
            async def apagar_recomendacao_later(chat_id, message_id):
                await asyncio.sleep(1200)
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except:
                    pass

            context.application.create_task(
                apagar_recomendacao_later(msg.chat_id, msg.message_id)
            )
        except:
            pass

    await update.message.reply_text("✅ Recomendações postadas nos grupos!")
    return ConversationHandler.END

# 4️⃣ Registro no ApplicationBuilder
live_conv = ConversationHandler(
    entry_points=[ CommandHandler('live', live, filters=filters.ChatType.PRIVATE) ],
    states={
        LIVE_LINK:   [
            MessageHandler(filters.TEXT & ~filters.COMMAND, live_receive_link),
            # você já tem aqui o cancelar via regex, mas não é estritamente necessário
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        LIVE_MOEDAS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, live_receive_moedas),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancel)],
    allow_reentry=True,
)


MIN_PONTOS_PARA_VOTAR = 16

async def tratar_voto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    voter_id  = query.from_user.id

    #Corte por ID
    if voter_id >= 7567101130:
        return await query.answer(
            "❌ Desculpe, este perfil é muito novo para votar..",
            show_alert=True
        )

    # ✅ Verifica se tem pontos suficientes
    pontos_atuais = await pool.fetchval(
        "SELECT pontos FROM usuarios WHERE user_id = $1",
        voter_id
    ) or 0

    if pontos_atuais < MIN_PONTOS_PARA_VOTAR:
        faltam = MIN_PONTOS_PARA_VOTAR - pontos_atuais
        return await query.answer(
            f"🔒 Você precisa de pelo menos {MIN_PONTOS_PARA_VOTAR} pontos para votar.\n"
            f"Faltam {faltam} ponto(s). Interaja com o bot para ganhar mais pontos. /como_ganhar",
            show_alert=True
        )

    # 1️⃣ Extração de rec_id e voto
    _, rec_id_str, voto_str = query.data.split(":")
    rec_id, voto = int(rec_id_str), bool(int(voto_str))

    # 2️⃣ Verifica se o usuário está bloqueado por penalização
    pen = await pool.fetchrow(
        "SELECT strikes, bloqueado_ate FROM penalizacoes WHERE user_id = $1",
        voter_id
    )
    agora = hoje_hora_sp()
    if pen and pen["bloqueado_ate"] and pen["bloqueado_ate"] > agora:
        return await query.answer(
            f"⛔ Você está impedido de votar até "
            f"{pen['bloqueado_ate'].strftime('%d/%m/%Y %H:%M')}.",
            show_alert=True
        )

    # 3️⃣ Busca recomendação
    rec = await pool.fetchrow(
        "SELECT user_id, moedas FROM recomendacoes WHERE id = $1",
        rec_id
    )
    if not rec:
        return await query.answer("❌ Recomendação não encontrada.", show_alert=True)
    if rec["user_id"] == voter_id:
        return await query.answer("❌ Você não pode votar em si mesmo.", show_alert=True)

    # 4️⃣ Verifica limite de 10 votos
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM recomendacao_votos WHERE rec_id = $1",
        rec_id
    )
    if total >= 10:
        return await query.answer(
            "❌ Já existem 10 votos. Período de votação encerrado.",
            show_alert=True
        )

    # 5️⃣ Verifica voto duplicado
    dup = await pool.fetchval(
        "SELECT 1 FROM recomendacao_votos WHERE rec_id = $1 AND voter_id = $2",
        rec_id, voter_id
    )
    if dup:
        return await query.answer("❌ Você já votou aqui.", show_alert=True)

    # 6️⃣ Grava o voto
    await pool.execute(
        "INSERT INTO recomendacao_votos (rec_id, voter_id, voto) VALUES ($1, $2, $3)",
        rec_id, voter_id, voto
    )

    # 7️⃣ Popup de conscientização
    texto_alert = (
        "Atenção, veja a live antes de votar. só ganha pontos o voto da maioria. 3 votos errados pode impedir de votar"
    )
    await query.answer(texto_alert, show_alert=True)

    # 8️⃣ Agenda revelação uma única vez
    flag = f"reveal_scheduled:{rec_id}"
    if not context.bot_data.get(flag):
        context.bot_data[flag] = True
        chat_id    = query.message.chat.id
        message_id = query.message.message_id

        async def revelar():
            await asyncio.sleep(360)  # tempo para reveçar os votos

            votos = await pool.fetch(
                "SELECT voto FROM recomendacao_votos WHERE rec_id = $1",
                rec_id
            )
            positivos = sum(1 for v in votos if v["voto"])
            negativos = len(votos) - positivos

            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"👍 {positivos}", callback_data="noop"),
                InlineKeyboardButton(f"👎 {negativos}", callback_data="noop"),
            ]])
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=teclado
                )
            except:
                pass

        context.application.create_task(revelar())

    # 9️⃣ Conta votos para decidir empate, pontos e penalização
    votos = await pool.fetch(
        "SELECT voto FROM recomendacao_votos WHERE rec_id = $1", rec_id
    )
    positivos = sum(1 for v in votos if v["voto"])
    negativos = len(votos) - positivos

    # 9.1️⃣ Empate: nem pontos, nem penalização
    if len(votos) >= 3 and positivos == negativos:
        return await query.answer(
            "⚖️ Houve empate na votação: nenhum ponto é dado e ninguém é penalizado.",
            show_alert=True
        )

    # 9.2️⃣ Maioria positiva: concede pontos
    if len(votos) >= 3 and positivos > negativos:
        pontos = rec["moedas"] * 10
        await atualizar_pontos(rec["user_id"], pontos, "Live aprovada")
        # aqui você pode inserir também a lógica de atualizar ranking separado e notificar o autor
        return

    # 9.3️⃣ Maioria negativa: penaliza quem votou contra a maioria
    if len(votos) >= 3 and negativos > positivos:
        # incrementa strike
        row = await pool.fetchrow(
            """
            INSERT INTO penalizacoes (user_id, strikes)
            VALUES ($1, 1)
            ON CONFLICT (user_id)
            DO UPDATE SET strikes = penalizacoes.strikes + 1
            RETURNING strikes
            """,
            voter_id
        )
        strikes = row["strikes"]
        # se atingir 3 strikes, bloqueia por 3 dias
        if strikes >= 3:
            bloqueado_ate = agora + timedelta(days=3)
            await pool.execute(
                "UPDATE penalizacoes SET bloqueado_ate = $1 WHERE user_id = $2",
                bloqueado_ate, voter_id
            )
            return await query.answer(
                "⛔ Você recebeu 3 strikes por votar contra a maioria "
                "e está bloqueado de votar por 3 dias.",
                show_alert=True
            )



async def atualizar_ranking_recomendacoes(user_id: int, pontos: int):
    await pool.execute(
        """
        INSERT INTO ranking_recomendacoes (user_id, pontos)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET pontos = ranking_recomendacoes.pontos + EXCLUDED.pontos
        """,
        user_id, pontos
    )

async def ranking_lives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await pool.fetch(
        """
        SELECT r.user_id, r.pontos,
               u.first_name, u.username, u.nickname, u.display_choice
        FROM ranking_recomendacoes r
        JOIN usuarios u ON u.user_id = r.user_id
        ORDER BY r.pontos DESC
        LIMIT 8
        """
    )

    if not rows:
        await update.message.reply_text("📭 Nenhuma live aprovada ainda.")
        return

    texto = "🏆 *Ranking de Lives Aprovadas*\n\n"
    medalhas = ["🥇", "🥈", "🥉"] + ["🏅"] * 5

    for i, row in enumerate(rows):
        nome = (
            row["first_name"] if row["display_choice"] == "first_name" else
            row["nickname"] if row["display_choice"] == "nickname" else
            row["username"] or "Usuário"
        )
        texto += f"{medalhas[i]} *{nome}* — {row['pontos']} pts\n"

    await update.message.reply_text(texto, parse_mode="Markdown")

async def on_startup(app):
    global ADMINS
    await init_db_pool()
    ADMINS = await carregar_admins_db()
    logger.info(f"Admins carregados: {ADMINS}")


main_conv = ConversationHandler(
    entry_points=[
        CommandHandler("admin", admin, filters=filters.ChatType.PRIVATE),
        CommandHandler("add", add_pontos),
        CommandHandler("del", del_pontos),
        CommandHandler("rem_admin", rem_admin),
    ],
    states={
        # /admin → senha
        ADMIN_SENHA: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, tratar_senha),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        # /add_pontos → id, qtd, motivo
        ADD_PONTOS_POR_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_IDuser),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        ADD_PONTOS_QTD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_quantidade),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        ADD_PONTOS_MOTIVO: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_pontos_motivo),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],

        DEL_PONTOS_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_IDuser),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        DEL_PONTOS_QTD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_quantidade),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        DEL_PONTOS_MOTIVO: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, del_pontos_motivo),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
        # # /add_admin → id
        REM_ADMIN_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, rem_admin_execute),
            MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancel)],
    allow_reentry=True,
)


# --- Inicialização do bot ---
async def main():
    global ADMINS

    # 1) inicializa o pool ANTES de criar o Application
    await init_db_pool()
    ADMINS = await carregar_admins_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )
    app.add_handler(main_conv)
    app.add_handler(live_conv)
    app.add_handler(CallbackQueryHandler(tratar_voto, pattern=r"^voto:\d+:[01]$"))
    app.add_handler(CommandHandler("registrar", registrar_grupo, filters=filters.ChatType.GROUPS))


    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start, filters=filters.ChatType.PRIVATE)],
            states={
                ESCOLHENDO_DISPLAY: [
                    CallbackQueryHandler(tratar_display_choice, pattern=r"^set:")
                ],
                DIGITANDO_NICK: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receber_nickname),
                    MessageHandler(filters.Regex(r'^(cancelar|/cancelar)$'), cancel)
                ],
            },
            fallbacks=[CommandHandler("cancelar", cancel)],
            allow_reentry=True,
        )
    )

    app.add_handler(CommandHandler("inicio", cmd_inicio, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('admin', admin, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('meus_pontos', meus_pontos))
    app.add_handler(CommandHandler('historico', historico, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_historico, pattern=r"^hist:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(paginacao_via_start, pattern=r"^via_start:\d+$"))

    app.add_handler(CommandHandler('rank_top10', ranking_top10))
    app.add_handler(CommandHandler("rank_lives", ranking_lives))
    app.add_handler(CommandHandler("historico_user", historico_usuario, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listar_usuarios", listar_usuarios, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listar_via_start", listar_via_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("estatisticas", estatisticas, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('como_ganhar', como_ganhar, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("news", news, filters=filters.ChatType.PRIVATE))

    # Presença em grupos
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, tratar_presenca))

    logger.info("🔄 Iniciando polling...")
    await app.run_polling()


if __name__ == "__main__":

    try:
        nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())
    except Exception:
        logger.exception("❌ Erro durante run_polling")
