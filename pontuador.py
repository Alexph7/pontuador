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
from telegram.constants import ParseMode
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

#Estados da conversa
(ADMIN_SENHA, ESPERANDO_SUPORTE, ADD_PONTOS_POR_ID, ADD_PONTOS_QTD, ADD_PONTOS_MOTIVO, DEL_PONTOS_ID, DEL_PONTOS_QTD,
 DEL_PONTOS_MOTIVO, ADD_ADMIN_ID, REM_ADMIN_ID, REMOVER_PONTUADOR_ID, BLOQUEAR_ID, BLOQUEAR_MOTIVO, DESBLOQUEAR_ID,
 ADD_PALAVRA_PROIBIDA, DEL_PALAVRA_PROIBIDA) = range(16)

hoje = hoje_sp()

TEMPO_LIMITE_BUSCA = 10          # Tempo máximo (em segundos) para consulta


async def init_db_pool():
    global pool, ADMINS
    pool = await asyncpg.create_pool(dsn=DATABASE_URL,min_size=1,max_size=10)
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
            display_choice     VARCHAR(20) NOT NULL DEFAULT 'indefinido',
            nickname           VARCHAR(50) NOT NULL DEFAULT 'sem nick'
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

        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        );

       CREATE TABLE IF NOT EXISTS usuario_history (
            id           SERIAL    PRIMARY KEY,
            user_id      BIGINT    NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            status       TEXT      NOT NULL,         -- 'Inserido' ou 'Atualizado'
            username     TEXT      NOT NULL DEFAULT 'vazio',
            first_name   TEXT      NOT NULL DEFAULT 'vazio',
            last_name    TEXT      NOT NULL DEFAULT 'vazio',
            inserido_em  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            display_choice  VARCHAR(20) NOT NULL DEFAULT 'indefinido',
            nickname        VARCHAR(50) NOT NULL DEFAULT 'sem nick'
        );
        """)
    ADMINS = await carregar_admins_db()

# --- Helpers de usuário (asyncpg) ---
PAGE_SIZE = 22
MAX_MESSAGE_LENGTH = 4000
HISTORICO_USER_ID = 4


async def adicionar_usuario_db(
    user_id: int,
    username: str = "vazio",
    first_name: str = "vazio",
    last_name: str = "vazio",
        display_choice: str = "indefinido",
        nickname: str = "sem nick",
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
                        f"firstname: {first_name} lastname: {last_name}"
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
                            VALUES ($1::bigint, 1, 'ponto diário por interação')
                            """,
                            user_id
                        )
            else:
                logger.info(
                    f"[DB] {user_id} Inserido: username: {username} "
                    f"firstname: {first_name} lastname: {last_name}"
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
                    VALUES ($1, 1, 'ponto diário por interação')
                    """,
                    user_id
                )


async def obter_ou_criar_usuario_db(user_id: int, username: str, first_name: str, last_name: str):
    perfil = await pool.fetchrow("SELECT * FROM usuarios WHERE user_id = $1", user_id)
    if perfil is None:
        await pool.execute(
            "INSERT INTO usuarios (user_id, username, first_name, last_name, display_choice, nickname) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            user_id, username or "vazio", first_name or "vazio", last_name or "vazio", "indefinido", "sem nick"
    )
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

        ]

        # 1) Comandos padrão (público)
        await app.bot.set_my_commands(
            comandos_basicos,
            scope=BotCommandScopeDefault()
        )

        # 2) Comandos em chat privado (com suporte)
        comandos_privados = comandos_basicos + [
            BotCommand("historico", "Mostrar seu histórico de pontos"),
            BotCommand("como_ganhar", "Como ganhar mais pontos"),
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
        ADMINS.add(user_id)                # Salva na memória enquanto o bot roda
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


ESCOLHENDO_DISPLAY, DIGITANDO_NICK = range(2)

# Handler para /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 1) Verifica se já existe registro; só insere uma vez
    await obter_ou_criar_usuario_db(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio"
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
            nickname="sem nick",
        )
        await query.edit_message_text("👍 Ok, você aparecerá com seu nome normal, para prosseguir escolha uma opção no menú ao lado.")
        return ConversationHandler.END

    # 2️⃣ Se for “nickname”, pede o nick e vai pro estado DIGITANDO_NICK
    if escolha == "nickname":
        await query.edit_message_text("✏️ Digite agora o nickname que você quer usar:")
        return DIGITANDO_NICK

    # 3️⃣ Se for “anonymous”, gera inicial com fallback zero e salva
    if escolha == "anonymous":
        # tenta first_name, senão username, senão '0'
        if user.first_name and user.first_name.strip():
            inicial = user.first_name.strip()[0]
        elif user.username and user.username.strip():
            inicial = user.username.strip()[0]
        else:
            inicial = "0"

        anon = f"{inicial.upper()}****"

        await adicionar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
            display_choice="anonymous",
            nickname=anon,  # salva “0*****” ou “A*****”
        )

        await query.edit_message_text(
            f"✅ Você ficará anônimo como <code>{anon}</code>.\n\n"
            "Agora escolha uma opção no menu ao lado.",
            parse_mode=ParseMode.HTML
        )
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
        perfil = await obter_ou_criar_usuario_db(
            user_id=user.id,
            username=user.username or "vazio",
            first_name=user.first_name or "vazio",
            last_name=user.last_name or "vazio",
        )

        pontos = perfil['pontos']
        nivel = perfil['nivel_atingido']

        if nivel == 0:
            nivel_texto = "rumo ao Nível 1"
        else:
            nivel_texto = f"Eba ja alcançou brinde de Nível {nivel}"

        await update.message.reply_text(
            f"🎉 Você tem {pontos} pontos {nivel_texto}."
        )

    except Exception as e:
        logger.error(f"Erro ao buscar pontos do usuário {user.id}: {e}", exc_info=True)
    await update.message.reply_text(
            "❌ Desculpe, tivemos um problema ao acessar as suas informações. "
            "Tente novamente mais tarde. Se o problema persistir contate o suporte."
    )

async def como_ganhar(update: Update, context: CallbackContext):
    # Ordena os brindes por nível de pontos
    brindes_texto = "\n".join(
        f"• {pontos} pontos – {descricao}"
        for pontos, descricao in sorted(NIVEIS_BRINDES.items())
    )
    await update.message.reply_text(
        "🎯 Você Pode Ganha Pontos Por:\n\n"
        "• Compras por ID em videos.\n"
        "• Até 1 comentário diario em grupos ou interação com bot\n"
        "• Muito cedo, mais opções de como ganhar pontos aparecerá em breve. \n\n"
        "💸 Como Você Pode Perder Pontos:\n"
        "• Trocas por brindes, desconta os pontos.\n"
        "• troca de ciclo ou fim do evento, os pontos zeram\n"
        "• Comportamento spamming, banimento\n"
        "• Produto devolvido (se aplicar)\n\n"
         f"{brindes_texto}\n\n"
        "Use /meus_pontos para ver seu total!"
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
    pontos   = context.user_data.pop("add_pt_value")
    motivo   = context.user_data.pop("add_pt_reason")

    novo_total = await atualizar_pontos(alvo_id, pontos, motivo, context.bot)
    await update.message.reply_text(
        f"✅ {pontos} pts atribuídos a {alvo_id}.\n"
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
        username = chat.username or ""
        first_name = chat.first_name or ""
        last_name = chat.last_name or ""
    except Exception:
        username = ""
        first_name = ""
        last_name = ""

    usuario = await obter_ou_criar_usuario_db(
        user_id, username, first_name, last_name
    )
    if not usuario:
        return None

    pontos_atuais = usuario['pontos'] or 0
    novos = pontos_atuais + delta
    nivel = usuario['nivel_atingido'] or 0

    await registrar_historico_db(user_id, delta, motivo)

    for limiar in NIVEIS_BRINDES.keys():
        if pontos_atuais < limiar <= novos:
            nivel += 1
            break

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
         LIMIT 60
        """,
        user.id
    )
    if not rows:
        await update.message.reply_text("🗒️ Nenhum registro de histórico encontrado.")
        return
    lines = [
        f" {r['data'].strftime('%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}"
        for r in rows
    ]
    await update.message.reply_text("🗒️ Seu histórico de pontos:\n\n" + "\n\n".join(lines))


async def ranking_top10(update: Update, context: CallbackContext):
    user = update.effective_user

    # 🔍 1) Verifica se o próprio usuário pode ver o ranking
    perfil = await pool.fetchrow(
        "SELECT display_choice FROM usuarios WHERE user_id = $1", user.id
    )

    if perfil is None:
        await update.message.reply_text(
            "⚠️ Você ainda não está cadastrado. Use /start para se cadastrar."
        )
        return

    if perfil["display_choice"] == "indefinido":
        await update.message.reply_text(
            "⚠️ Para acessar o ranking, primeiro escolha como seu nome deve aparecer.\n\n"
            "Use /start para fazer essa escolha."
        )
        return

    # 🏅 2) Busca os top 10 que já escolheram como aparecer
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
        WHERE display_choice != 'indefinido'
        ORDER BY pontos DESC
        LIMIT 10
        """
    )

    if not top:
        await update.message.reply_text("🏅 Nenhum usuário cadastrado no ranking.")
        return

    # 🏆 3) Monta o texto com base na escolha de display de cada um
    linhas = ["🏅 Top 10 de pontos:"]
    for i, u in enumerate(top):
        choice = u["display_choice"]
        if choice == "first_name":
            display = u["first_name"] or u["username"] or "Usuário"
        elif choice in ("nickname", "anonymous"):
            display = u["nickname"] or u["username"] or "Usuário"
        else:
            display = u["username"] or u["first_name"] or "Usuário"

        linhas.append(f"{i + 1}. {display.upper()} – {u['pontos']} pts")

    texto = "\n\n".join(linhas)
    await update.message.reply_text(texto)


async def tratar_presenca(update, context):
    user = update.effective_user

    # 1) Garante que exista sem logar toda vez
    await adicionar_usuario_db(user_id=user.id, username=user.username or "vazio",
                               first_name=user.first_name or "vazio", last_name=user.last_name or "vazio",
                               display_choice="anonymous", nickname="sem nick")
    # 2) Busca registro completo
    reg = await obter_ou_criar_usuario_db(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio"
    )

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

    if requester_id not in ADMINS:
        await update.message.reply_text("🔒 Você precisa autenticar: use /admin primeiro.")
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
            "Uso incorreto. Digite `/historico_usuario ajuda`",
            parse_mode="MarkdownV2"
        )
        return ConversationHandler.END

    offset = (page - 1) * PAGE_SIZE

    if target_id is None:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, display_choice, nickname, inserido_em"
            " FROM usuario_history"
            " ORDER BY inserido_em DESC, id DESC"
            " LIMIT $1 OFFSET $2"
        )
        params = (PAGE_SIZE + 1, offset)
        header = f"🕒 Histórico completo (todos os usuários, página {page}):\n"
    else:
        sql = (
            "SELECT id, user_id, status, username, first_name, last_name, display_choice, nickname, inserido_em"
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
            f"display_choice: `{escape_markdown_v2(r['display_choice'])}`"
            f"nickname: `{escape_markdown_v2(r['nickname'])}`"
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
                callback_data=f"hist:{target_id or 0}:{page-1}"
            )
        )
    if tem_mais:
        botoes.append(
            InlineKeyboardButton(
                "Próximo ▶️",
                callback_data=f"hist:{target_id or 0}:{page+1}"
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


async def on_startup(app):
    global ADMINS
    await init_db_pool()
    ADMINS = await carregar_admins_db()
    logger.info(f"Admins carregados: {ADMINS}")


main_conv = ConversationHandler(
    entry_points=[
        CommandHandler("admin", admin, filters=filters.ChatType.PRIVATE),
        CommandHandler("add_pontos", add_pontos),
        # CommandHandler("del_pontos", del_pontos),
        # CommandHandler("add_admin", add_admin),
        # CommandHandler("rem_admin", rem_admin),
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
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
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
    # app.add_handler(
    #     CommandHandler("total_usuarios", total_usuarios)
    # )

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