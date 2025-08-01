import os
import re
import sys
from random import random
from urllib.parse import urlparse
import asyncpg
import logging
import random
import nest_asyncio
import asyncio
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, User
from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler
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
LIMIAR_PONTUADOR = 500
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

ADMINS = set()

# Se quiser suportar um admin “principal”:
id_admin_env = os.getenv("ID_ADMIN", "").strip()
if id_admin_env:
    try:
        ADMINS.add(int(id_admin_env))
    except ValueError:
        logger.error("ID_ADMIN inválido, deve ser um número inteiro único")

# Depois, a lista extra de admins:
admin_ids_env = os.getenv("ADMIN_IDS", "")
if admin_ids_env:
    try:
        ADMINS.update({int(x.strip()) for x in admin_ids_env.split(',') if x.strip()})
    except ValueError:
        logger.error("ADMIN_IDS deve conter apenas números separados por vírgula.")
logger.info(f"🛡️ Admins carregados da configuração: {ADMINS}")

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

ranking_mensagens_top = {}


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)

    async with pool.acquire() as conn:
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
        
       CREATE TABLE IF NOT EXISTS config_checkin (  
            chave TEXT PRIMARY KEY,  -- chave é adicionar pontos por checkin
            valor TEXT NOT NULL   -- valor é true para pontuar por checkin e false pra nao pontuar
        );

       CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        );

       -- tabela de canais para uso em sorteio_config
       CREATE TABLE IF NOT EXISTS canais (
            id   BIGINT PRIMARY KEY,
            nome TEXT
        );
        
       CREATE TABLE IF NOT EXISTS ganhadores_bloqueados (
            user_id BIGINT PRIMARY KEY,
            bloqueado_em TIMESTAMP DEFAULT NOW()
        );

                               
       CREATE TABLE IF NOT EXISTS sorteio_config (
            id                          SERIAL PRIMARY KEY,
            canal_id                    BIGINT REFERENCES canais(id) ON DELETE SET NULL,
            criado_em                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ativo                       BOOLEAN    NOT NULL DEFAULT TRUE,
        
            total_montante              NUMERIC    NOT NULL,
            valor_premio                NUMERIC    NOT NULL,
            premios_iniciais            INT        NOT NULL,
            premios_restantes           INT        NOT NULL,
        
            total_participantes_esperados INT     NOT NULL,  -- Nº base de participantes
            tentativas_por_usuario        INT     NOT NULL DEFAULT 3,  -- Nº de tentativas antes do cooldown
            cooldown_minutos              INT     NOT NULL DEFAULT 5,  -- Minutos de espera após esgotar
            tentativa_atual               INT     NOT NULL DEFAULT 0,  -- Contador de tentativas no evento
            numero_esperado_atual         INT     NOT NULL        -- Número que o usuário deve acertar
        );

       CREATE TABLE IF NOT EXISTS sorteio_tentativas (
            event_id INT NOT NULL REFERENCES sorteio_config(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            tentado_em TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (event_id, user_id, tentado_em)
        );

       CREATE TABLE IF NOT EXISTS sorteio_ganhadores (
            event_id INT NOT NULL REFERENCES sorteio_config(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            ganho_em TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (event_id, user_id)
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
                    VALUES ($1, $2, $3, $4, $5, $6, NOW(), NULL, 0, $7)
                    """,
                    user_id, username, first_name, last_name,
                    display_choice, nickname, via_start
                )

                await conn.execute(
                    """
                    INSERT INTO usuario_history
                      (user_id, status, username, first_name, last_name, display_choice, nickname, via_start)
                    VALUES ($1::bigint, 'Inserido', $2, $3, $4, $5, $6, $7)
                    """,
                    user_id, username, first_name, last_name, display_choice, nickname, via_start
                )


async def obter_ou_criar_usuario_db(
        user_id: int,
        username: str = "vazio",
        first_name: str = "vazio",
        last_name: str = "vazio",
        via_start: bool = False
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
            BotCommand("meus_pontos", "Sua pontuação e nível"),
            BotCommand("rank_tops", "Ranking pontuadores"),
            #BotCommand("sortear", "Sortear")

        ]

        # 1) Comandos padrão (público)
        await app.bot.set_my_commands(
            comandos_basicos,
            scope=BotCommandScopeDefault()
        )

        # 2) Comandos em chat privado (com suporte)
        comandos_privados = comandos_basicos + [
            BotCommand("inicio", "Volte ao começo"),
            BotCommand("historico", "Mostrar seu histórico"),
            BotCommand("list_pontuadores", "listar usuarios acima de 100 pontos"),
            BotCommand("como_ganhar", "Como ganhar pontos"),
            BotCommand("news", "Ver Atualizações"),
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
    ("/historico", "Mostrar seu histórico de pontos"),
    ("/rank_tops", "Ranking usuários por pontos"),
    ("/como_ganhar", "Como ganhar mais pontos"),
    ("/news", "Ver Novas Atualizações"),
]


async def enviar_menu(chat_id: int, bot):
    texto_menu = "👋 Tudo Certo! Aqui estão os comandos que você pode usar:\n\n"
    for cmd, desc in COMANDOS_PUBLICOS:
        texto_menu += f"{cmd} — {desc}\n"
    await bot.send_message(chat_id=chat_id, text=texto_menu)


# mantém enviar_menu(chat_id: int, bot: Bot) do jeito que você já definiu

async def cmd_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if update.effective_chat.type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    await enviar_menu(update.effective_chat.id, context.bot)


ADMIN_MENU = (
    "🔧 *Menu Admin* 🔧\n\n"
    "/add - atribuir pontos usuário\n"
    "/del – remover pontos de usuário\n"
    "/historico_usuario – historico de nomes de usuario\n"
    "/rem – remover admin\n"
    "/listar_usuarios – lista de usuarios cadastrados\n"
    "/estatisticas – quantidade total cadastrados\n"
    "/listar_via_start – que se cadastraram via start\n"
    "/checkin_on – ativa pontos no checkin\n"
    "/checkin_off – desativa pontos no checkin\n"
    "/configurar_sort – configurar novo sorteio\n"
    "/sort_status – ver status do sorteio\n"
    "/cancelar_sort – cancelar sorteio\n"
    "/list_ganhadores_sort – listar ganhadores atuais\n"
    "/backup – Fazer backup\n")


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

    user_id = user.id
    username = user.username or "vazio"
    first_name = user.first_name or "vazio"
    last_name = user.last_name or "vazio"

    logger.info(f"[start] Início para user_id={user_id}, username={username}, first_name={first_name}")

    # 🔒 Verifica se está no canal
    ok, msg = await verificar_canal(user.id, context.bot)
    logger.info(f"[start] verificar_canal para user_id={user_id} resultado: {ok}")
    if not ok:
        await update.message.reply_text(msg)
        return ConversationHandler.END

    # Checa valor da configuração 'adicionar_pontos'
    config_checkin = await pool.fetchrow("SELECT valor FROM config_checkin WHERE chave = 'adicionar_pontos'")
    if config_checkin:
        logger.info(f"[start] Config_checkin 'adicionar_pontos' = {config_checkin['valor']}")
    else:
        logger.warning("[start] Config_checkin 'adicionar_pontos' não encontrada")

    # 1) Verifica se já existe registro; só insere uma vez
    perfil = await obter_ou_criar_usuario_db(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        via_start=True
    )
    logger.info(f"[start] Perfil obtido/criado: {perfil}")

    await processar_presenca_diaria(
        perfil=perfil,  # passa o perfil direto
        bot=context.bot
    )

    # Pergunta como ele quer aparecer
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Mostrar nome do jeito que está", callback_data="set:first_name")],
        [InlineKeyboardButton("2️⃣ Escolher um Nick/Apelido", callback_data="set:nickname")],
        [InlineKeyboardButton("3️⃣ Ficar anônimo", callback_data="set:anonymous")],
    ])
    await update.message.reply_text(
        f"🤖 Bem-vindo, {user.first_name}! Ao Prosseguir você aceita os termos de uso do bot \n\n"
        f"Para começar, caso você alcance o Ranking, como você gostaria de aparecer?",
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
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    # Validação de canal se for no privado
    if update.effective_chat.type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    # Usa a função reutilizável para verificar se o usuário está no canal
    ok, msg = await verificar_canal(user_id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return

    try:
        # 1) Processa presença diária (vai dar 1 ponto se ainda não pontuou hoje)
        perfil = await obter_ou_criar_usuario_db(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name
        )

        await processar_presenca_diaria(perfil, context.bot)

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
            nivel_texto = "Rumo ao nível 1"
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

    if update.effective_chat.type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    # Usa a função reutilizável para verificar se o usuário está no canal
    ok, msg = await verificar_canal(user_id, context.bot)
    if not ok:
        await update.message.reply_text(msg)
        return

    texto = (
        "🎯* Ultima Interação Válida a Partir de 1 de Maio de 2025 a 30 de Junho*\n\n"
        "Interações terminadas, em breve novas atualizações"

    )

    await update.message.reply_text(texto, parse_mode="Markdown")


async def news(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if update.effective_chat.type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    await update.message.reply_text(
        "🆕 *Novidades* ( -- 2025)\n\n"
        "Novidades em Breve",
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
        logger.warning(f"Usuário {user_id} não encontrado para atualizar pontos")
        return None

    pontos_atuais = usuario['pontos'] or 0
    novos = pontos_atuais + delta
    logger.info(f"[atualizar_pontos] user_id={user_id} pontos_atuais={pontos_atuais} delta={delta} novos={novos}")

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
    logger.info(f"[atualizar_pontos] Pontos atualizados no banco para user_id={user_id}")
    return novos


async def historico(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = user.id
    chat_type = update.effective_chat.type

    if chat_type == "private":
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return
    rows = await pool.fetch(
        """
        SELECT data, pontos, motivo
          FROM historico_pontos
         WHERE user_id = $1
      ORDER BY data DESC
         LIMIT 50
        """,
        user_id
    )

    if not rows:
        await update.message.reply_text("🗒️ Nenhum registro de histórico encontrado.")
        return

    lines = [
        f" {format_dt_sp(r['data'], '%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}"
        for r in rows
    ]
    await update.message.reply_text("🗒️ Seu histórico de pontos:\n\n" + "\n\n".join(lines))


async def ranking_tops(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    chat_id = update.effective_chat.id

    # 1) Presença diária unificada — agora com 5 args:
    perfil = await obter_ou_criar_usuario_db(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name
    )

    if update.effective_chat.type == "private":
        await processar_presenca_diaria(
            perfil=perfil,
            bot=context.bot
        )
        invalido, msg = await perfil_invalido_ou_nao_inscrito(user_id, context.bot)
        if invalido:
            await update.message.reply_text(msg)
            return

    mensagem_antiga_id = ranking_mensagens_top.get(chat_id)
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
        LIMIT 20
        """
    )

    if not top:
        msg = await update.message.reply_text("🏅 Nenhum usuário cadastrado no ranking.")
        ranking_mensagens_top[chat_id] = msg.message_id
        return

    linhas = ["🏆 <b>Ranking Geral Top 20</b>\n"]
    medalhas = ["🥇", "🥈", "🥉"] + ["🏅"] * 17

    for i, u in enumerate(top):
        choice = u["display_choice"]
        if choice == "first_name":
            display = u["first_name"] or u["username"] or "Usuário"
        elif choice in ("nickname", "anonymous"):
            display = u["nickname"] or u["username"] or "Usuário"
        elif choice == "indefinido":
            display = "Esp. interação"
        else:
            display = u["username"] or u["first_name"] or "Usuário"

        pontos = u["pontos"]
        medalha = medalhas[i] if i < len(medalhas) else "🎖️"

        linhas.append(f"{medalha} <b>{display}</b> — <code>{pontos} pts</code>")

    texto = "\n".join(linhas)

    msg = await update.message.reply_text(
        texto,
        parse_mode=ParseMode.HTML  # ou telegram.constants.ParseMode.HTML, dependendo da sua versão
    )

    ranking_mensagens_top[chat_id] = msg.message_id


async def tratar_presenca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.is_bot:
        return

    perfil = await obter_ou_criar_usuario_db(
        user_id=user.id,
        username=user.username or "vazio",
        first_name=user.first_name or "vazio",
        last_name=user.last_name or "vazio"
    )

    await processar_presenca_diaria(perfil, context.bot)

logger = logging.getLogger(__name__)

async def processar_presenca_diaria(perfil: asyncpg.Record | dict, bot: Bot) -> int | None:
    logger.info(
        f"[processar_presenca_diaria] user_id={perfil['user_id']} última interação: {perfil['ultima_interacao']}")

    resultado = await pool.fetchrow("SELECT valor FROM config_checkin WHERE chave = 'adicionar_pontos'")
    if not resultado or resultado["valor"] != "true":
        logger.info("[processar_presenca_diaria] Check-in desativado na configuração")
        return None

    user_id = perfil["user_id"]
    ultima_interacao = perfil["ultima_interacao"]

    if ultima_interacao != hoje_data_sp():
        novo_total = await atualizar_pontos(user_id, 5, "Presença diária", bot)

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


PAGE_SIZE_LISTAR = 5

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



async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ Você não tem permissão para usar /backup.")
        return

    await update.message.reply_text("🔄 Gerando dump do banco... aguarde.")

    # Extrai dados da DATABASE_URL
    url = urlparse(os.getenv("DATABASE_URL"))
    host = url.hostname
    port = str(url.port or 5432)
    user = url.username
    pwd = url.password
    db = url.path.lstrip("/")

    # Gera nome e pasta do dump
    ts = datetime.now(tz=ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d_%H%M%S")
    nome = f"dump_{ts}.sql"
    pasta = os.getenv("BACKUP_DIR", "./backups")
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, nome)

    # Comando pg_dump plain SQL
    cmd = [
        "pg_dump",
        "-h", host,
        "-p", port,
        "-U", user,
        "-d", db,
        "-F", "p",
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = pwd

    # Executa dump
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, env=env)
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        await update.message.reply_text("❌ Erro ao gerar dump. Veja os logs do servidor.")
        return

    # Grava arquivo
    with open(caminho, "wb") as f:
        f.write(stdout)

    # Informa no chat e, se pequeno, envia o arquivo
    tamanho = os.path.getsize(caminho)
    msg = f"✅ Dump gerado em:\n`{caminho}`"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    if tamanho < 50 * 1024 * 1024:
        await update.message.reply_document(document=InputFile(caminho), filename=nome)


async def ativar_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pool.execute(
        "INSERT INTO config_checkin (chave, valor) VALUES ('adicionar_pontos', 'true') "
        "ON CONFLICT (chave) DO UPDATE SET valor = 'true'"
    )
    await update.message.reply_text("✅ Check-in ativado. Usuários agora ganham pontos.")


async def desativar_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pool.execute(
        "INSERT INTO config_checkin (chave, valor) VALUES ('adicionar_pontos', 'false') "
        "ON CONFLICT (chave) DO UPDATE SET valor = 'false'"
    )
    await update.message.reply_text("❌ Check-in desativado. Nenhum usuário ganhará pontos.")

# Estados do ConversationHandler
CONFIG_SORTEIO_MONTANTE = 1
CONFIG_SORTEIO_PREMIO = 2
CONFIG_SORTEIO_QTD_PARTICIPANTES = 3
CONFIG_SORTEIO_TENTATIVAS_POR_USUARIO = 4
CONFIG_SORTEIO_COOLDOWN = 5
CONFIG_SORTEIO_CONFIRMACAO = 6

# Timeout e constantes
COOLDOWN_MINUTOS = 5
async def setar_canal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type not in ["channel", "group", "supergroup"]:
        await update.message.reply_text("Este comando deve ser usado em um canal ou grupo.")
        return

    canal_id = chat.id
    nome = chat.title or chat.username or str(canal_id)

    # Exemplo: salvando no banco
    await pool.execute(
        "INSERT INTO canais (id, nome) VALUES ($1, $2) "
        "ON CONFLICT (id) DO UPDATE SET nome = EXCLUDED.nome",
        canal_id, nome
    )
    context.bot_data["canal_id"] = canal_id
    await update.message.reply_text(f"✅ Canal/grupo registrado.")

async def configurar_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🎁 Qual o montante total a distribuir em reais?")
    return CONFIG_SORTEIO_MONTANTE

async def receber_montante(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        montante = float(update.message.text.replace(",", "."))
        if montante <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Valor inválido. Envie apenas um número maior que zero.")
        return CONFIG_SORTEIO_MONTANTE

    context.user_data["montante"] = montante
    await update.message.reply_text("💰 Qual o valor de cada prêmio (em R$)?")
    return CONFIG_SORTEIO_PREMIO

async def receber_valor_premio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        valor = float(update.message.text.replace(",", "."))
        if valor <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Valor inválido. Envie apenas um número maior que zero.")
        return CONFIG_SORTEIO_PREMIO

    context.user_data["valor_premio"] = valor
    await update.message.reply_text(
        "👥 Quantos participantes participarão do sorteio?\n\n"
        "_Base usada para definir tentativas e sorteios sequenciais._",
        parse_mode="Markdown"
    )
    return CONFIG_SORTEIO_QTD_PARTICIPANTES

async def receber_qtd_participantes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        qtd = int(update.message.text)
        if qtd <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Envie um número válido de participantes.")
        return CONFIG_SORTEIO_QTD_PARTICIPANTES

    context.user_data["qtd_participantes"] = qtd
    await update.message.reply_text("🔁 Quantas tentativas cada participante terá?")
    return CONFIG_SORTEIO_TENTATIVAS_POR_USUARIO

async def receber_tentativas_por_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        tentativas = int(update.message.text)
        if tentativas <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Envie um número válido maior que zero.")
        return CONFIG_SORTEIO_TENTATIVAS_POR_USUARIO

    context.user_data["tentativas_por_usuario"] = tentativas
    await update.message.reply_text("⏱ Qual o tempo de espera (em minutos) entre tentativas?")
    return CONFIG_SORTEIO_COOLDOWN

async def receber_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cooldown = int(update.message.text)
        if cooldown < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Envie um número válido (em minutos).")
        return CONFIG_SORTEIO_COOLDOWN

    context.user_data["cooldown"] = cooldown

    montante = context.user_data["montante"]
    valor_premio = context.user_data["valor_premio"]
    qtd_premios = int(montante // valor_premio)
    context.user_data["qtd_premios"] = qtd_premios

    resumo = (
        f"*Resumo do sorteio:*\n"
        f"• Montante total: R${montante:.2f}\n"
        f"• Valor por prêmio: R${valor_premio:.2f}\n"
        f"• Prêmios totais: {qtd_premios}\n"
        f"• Participantes: {context.user_data['qtd_participantes']}\n"
        f"• Tentativas por participante: {context.user_data['tentativas_por_usuario']}\n"
        f"• Cooldown entre tentativas: {context.user_data['cooldown']} minuto(s)\n\n"
        f"Confirma a criação do sorteio?"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Sim", callback_data="confirmar_sorteio"),
            InlineKeyboardButton("👎 Cancelar", callback_data="cancelar_sorteio"),
        ]
    ])
    await update.message.reply_text(resumo, reply_markup=keyboard, parse_mode="Markdown")
    return CONFIG_SORTEIO_CONFIRMACAO


async def confirmar_sorteio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    montante = context.user_data["montante"]
    valor_premio = context.user_data["valor_premio"]
    qtd_premios = context.user_data["qtd_premios"]
    canal_id = context.bot_data.get("canal_id")

    numero_sorteado = random.randint(1, context.user_data["qtd_participantes"])

    await context.bot_data["pool"].execute("""
        INSERT INTO sorteio_config
            (canal_id, criado_em, ativo, total_montante, valor_premio,
             premios_iniciais, premios_restantes, total_participantes_esperados,
             tentativas_por_usuario, cooldown_minutos, tentativa_atual, numero_esperado_atual)
        VALUES ($1, NOW(), TRUE, $2, $3, $4, $4, $5, $6, $7, 0, $8)
    """, canal_id, montante, valor_premio, qtd_premios,
         context.user_data["qtd_participantes"],
         context.user_data["tentativas_por_usuario"],
         context.user_data["cooldown"],
         numero_sorteado)

    # Limpa tentativas e ganhadores anteriores
    await context.bot_data["pool"].execute("DELETE FROM sorteio_tentativas")
    await context.bot_data["pool"].execute("DELETE FROM sorteio_ganhadores")

    await query.edit_message_text("✅ Sorteio configurado com sucesso!")
    return ConversationHandler.END

async def cancelar_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Desativa o sorteio ativo
    await context.bot_data["pool"].execute(
        "UPDATE sorteio_config SET ativo = FALSE WHERE ativo = TRUE"
    )
    # Limpa tentativas e ganhadores do evento desativado
    await context.bot_data["pool"].execute(
        "DELETE FROM sorteio_tentativas WHERE event_id = (SELECT id FROM sorteio_config ORDER BY criado_em DESC LIMIT 1)"
    )
    await context.bot_data["pool"].execute(
        "DELETE FROM sorteio_ganhadores WHERE event_id = (SELECT id FROM sorteio_config ORDER BY criado_em DESC LIMIT 1)"
    )
    # Notifica o admin
    await update.message.reply_text("❌ Sorteio vigente cancelado e dados limpos. Pronto para nova configuração.")


# Defina o chat de suporte logo após as importações
CHAT_ID_SUPORTE = -1002563145936  # substitua pelo seu ID real

async def sortear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    agora = datetime.now(tz=ZoneInfo("America/Sao_Paulo"))

    # Verifica se está bloqueado por sorteios anteriores
    # Verifica se está bloqueado por sorteios anteriores
    bloqueado = await context.bot_data["pool"].fetchval(
        "SELECT 1 FROM sorteio_bloqueados WHERE user_id = $1",
        user_id
    )
    if bloqueado:
        return await update.message.reply_text(
            "🚫 Você já ganhou recentemente espere algum tempo e será liberado novamente.")

    inscrito, msg = await verificar_canal(user_id, context.bot)
    if not inscrito:
        return await update.message.reply_text(msg)

    canal_id = context.bot_data.get("canal_id")
    if not canal_id:
        return await update.message.reply_text("❌ Canal de sorteio não configurado.")

    # Busca evento ativo
    evento = await context.bot_data["pool"].fetchrow(
        "SELECT * FROM sorteio_config WHERE ativo = TRUE ORDER BY criado_em DESC LIMIT 1"
    )
    if not evento:
        return await update.message.reply_text("❌ Nenhum sorteio configurado.")

    if evento["premios_restantes"] <= 0:
        return await update.message.reply_text("❌ Todos os prêmios já foram distribuídos.")

    # Verifica se usuário já ganhou
    ganhou = await context.bot_data["pool"].fetchval(
        "SELECT 1 FROM sorteio_ganhadores WHERE event_id = $1 AND user_id = $2",
        evento["id"], user_id
    )
    if ganhou:
        return await update.message.reply_text("⚠️ Você já ganhou neste evento.")

    # Busca última tentativa (ganhando ou não) do usuário
    ultima = await context.bot_data["pool"].fetchval(
        "SELECT MAX(tentado_em) FROM sorteio_tentativas WHERE user_id = $1 AND event_id = $2",
        user_id, evento["id"]
    )

    if ultima and ultima.tzinfo is None:
        ultima = ultima.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))

    if ultima and agora - ultima < timedelta(minutes=evento["cooldown_minutos"]):
        restante = timedelta(minutes=evento["cooldown_minutos"]) - (agora - ultima)
        minutos = int(restante.total_seconds() // 60) + 1
        return await update.message.reply_text(f"⏱ Aguarde {minutos} minuto(s) para tentar novamente.")

    await context.bot_data["pool"].execute(
        "INSERT INTO sorteio_tentativas (event_id, user_id, tentado_em) VALUES ($1, $2, $3)",
        evento["id"], user_id, agora
    )

    # Atualiza tentativa atual
    tentativa_atual = evento["tentativa_atual"] + 1
    await context.bot_data["pool"].execute(
        "UPDATE sorteio_config SET tentativa_atual = $1 WHERE id = $2",
        tentativa_atual, evento["id"]
    )

    # Verifica se acertou o número esperado
    if tentativa_atual == evento["numero_esperado_atual"]:
        await context.bot_data["pool"].execute(
            "INSERT INTO sorteio_ganhadores (event_id, user_id, ganho_em) VALUES ($1, $2, $3)",
            evento["id"], user_id, agora
        )
        await context.bot_data["pool"].execute(
            "UPDATE sorteio_config SET premios_restantes = premios_restantes - 1, tentativa_atual = 0, numero_esperado_atual = $1 WHERE id = $2",
            random.randint(1, evento["total_participantes_esperados"]),
            evento["id"]
        )

        # Nome do usuário com fallback
        if user.username:
            nome = f"@{user.username}"
        elif user.first_name:
            nome = user.first_name
        elif user.last_name:
            nome = user.last_name
        else:
            nome = "sem nick"

        # Link para a mensagem original (se possível)
        chat = update.effective_chat
        message = update.message
        if chat.type in ["group", "supergroup"] and chat.id < 0:
            msg_link = f"https://t.me/c/{str(chat.id)[4:]}/{message.message_id}"
            texto_admin = (
                f"🎉 {nome} ganhou R${evento['valor_premio']:.2f} no sorteio #{evento['id']}!\n"
                f"Prêmios restantes: {evento['premios_restantes'] - 1}\n"
                f"🔗 [Ver mensagem]({msg_link})"
            )
        else:
            texto_admin = (
                f"🎉 {nome} ganhou R${evento['valor_premio']:.2f} no sorteio #{evento['id']}!\n"
                f"Prêmios restantes: {evento['premios_restantes'] - 1}"
            )

        await context.bot.send_message(
            chat_id=CHAT_ID_SUPORTE,
            text=texto_admin,
            parse_mode="Markdown"
        )

        await context.bot_data["pool"].execute(
            "INSERT INTO sorteio_bloqueados (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id
        )

        nome_display = user.username or user.first_name or user.last_name or "sem nick"
        mensagem_publica = (
            f"🎉 {nome_display} ganhou R${evento['valor_premio']:.2f} no sorteio!\n"
        )
        await context.bot.send_message(chat_id=canal_id, text=mensagem_publica)

        return await update.message.reply_text(
            f"🎉 Parabéns! Você ganhou R${evento['valor_premio']:.2f}!\n"
            f"Prêmios restantes: {evento['premios_restantes'] - 1}"
        )

    return await update.message.reply_text(
        f"😔 Não foi dessa vez. Tente novamente em {evento['cooldown_minutos']} minutos!"
    )

async def liberar_ganhadores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMINS = context.bot_data.get("chat_admin", set())
    user_id = update.effective_user.id

    if user_id not in ADMINS:
        return await update.message.reply_text("❌ Apenas administradores podem usar este comando.")

    await context.bot_data["pool"].execute("DELETE FROM sorteio_bloqueados")
    await update.message.reply_text("✅ Todos os ganhadores foram liberados para participar novamente.")


async def sort_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estado = await context.bot_data["pool"].fetchrow(
        """
        SELECT premios_restantes, tentativa_atual, numero_esperado_atual
          FROM sorteio_config
         WHERE ativo = TRUE
         ORDER BY criado_em DESC
         LIMIT 1
        """
    )
    if not estado:
        return await update.message.reply_text("❌ Nenhum sorteio ativo.")
    await update.message.reply_text(
        f"📊 Prêmios restantes: {estado['premios_restantes']}\n"
        f"Tentativa atual: {estado['tentativa_atual']}\n"
        f"Número a acertar: {estado['numero_esperado_atual']}"
    )

async def list_ganhadores_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await context.bot_data["pool"].fetch(
        """
        SELECT user_id, ganho_em
          FROM sorteio_ganhadores
         WHERE event_id = (
             SELECT id FROM sorteio_config
              WHERE ativo = TRUE
              ORDER BY criado_em DESC
              LIMIT 1
         )
        """
    )
    if not rows:
        return await update.message.reply_text("🏆 Ainda não há ganhadores.")
    lista = "\n".join(f"- {r['user_id']} em {r['ganho_em']}" for r in rows)
    await update.message.reply_text(f"🏆 Ganhadores:\n{lista}")

# Quantos itens por página
PAGE_SIZE_RANKING = 50

async def listar_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) Determina a página (default = 1)
    args = context.args or []
    try:
        page = int(args[0]) if args else 1
    except ValueError:
        await update.message.reply_text("❌ Página inválida. Use /ranking <número>.")
        return
    if page < 1:
        page = 1

    # 2) Conta só quem tem ≥100 pontos ➡️
    total_usuarios = await pool.fetchval(
        "SELECT COUNT(*) FROM usuarios WHERE pontos >= $1",
        100
    )
    total_paginas = max(1, math.ceil(total_usuarios / PAGE_SIZE_RANKING))
    if page > total_paginas:
        await update.message.reply_text(
            f"ℹ️ A página {page} não existe. Só há {total_paginas} páginas."
        )
        return

    # 3) Busca a página atual, do maior para o menor ➡️
    offset = (page - 1) * PAGE_SIZE_RANKING
    rows = await pool.fetch(
        """
        SELECT user_id, pontos, display_choice, first_name, nickname
        FROM usuarios
        WHERE pontos >= $1
        ORDER BY pontos DESC
        LIMIT $2 OFFSET $3
        """,
        100, PAGE_SIZE_RANKING, offset
    )

    # 4) Monta o texto mostrando o display escolhido em /start
    lines = []
    for i, row in enumerate(rows, start=1):
        indice = offset + i
        # Se escolheu aparecer com o first_name, usamos ele; senão, o nickname salvo
        if row["display_choice"] == "first_name":
            display = row["first_name"]
        elif row["display_choice"] == "nickname":
            display = row["nickname"]
        elif row["display_choice"] == "anonymous":
            display = row["nickname"]
        else:  # 'indefinido' ou valor estranho
            display = "Esp. interação"
        lines.append(f"{indice}. {display} — {row['pontos']} pontos")
    header = (
        f"🏆 **Lista Usuarios (≥100 pontos) — página {page}/{total_paginas} "
        f"(total {total_usuarios})**\n\n"
    )
    texto = header + "\n".join(lines)

    # 5) Botões de navegação
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀️ Anterior", callback_data=f"ranking|{page-1}"))
    if page < total_paginas:
        buttons.append(InlineKeyboardButton("Próximo ▶️", callback_data=f"ranking|{page+1}"))
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None

    # 6) Envia ou edita mensagem
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            texto, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            texto, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )

# Callback para tratar os cliques
async def callback_listar_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()  # para parar o spinner
    # Extrai a nova página de "ranking|<n>"
    _, nova_pagina = update.callback_query.data.split("|")
    context.args = [nova_pagina]
    await listar_ranking(update, context)


async def on_startup(app):
    global ADMINS

    # inicializa o pool
    await init_db_pool()
    app.bot_data["pool"] = pool

    await pool.execute("""
        INSERT INTO config_checkin (chave, valor) VALUES ('adicionar_pontos', 'true')
        ON CONFLICT (chave) DO NOTHING
    """)

    # Carrega do banco e mescla (sem sobrescrever o que veio do .env)
    existing = await carregar_admins_db()
    ADMINS.update(existing)
    logger.info(f"🛡️ Admins após iniciar: {ADMINS}")
    app.bot_data["chat_admin"] = ADMINS

    # Busca canal do sorteio no banco e converte para int
    canal_id_str = await pool.fetchval("SELECT valor FROM config_checkin WHERE chave = 'sorteio_canal_id'")
    if canal_id_str:
        try:
            canal_id = int(canal_id_str)
            app.bot_data["canal_id"] = canal_id
            logger.info(f"[DEBUG on_startup] canal_id_str='{canal_id_str}', canal_id={canal_id} ({type(canal_id)})")
        except ValueError:
            logger.error(f"⚠️ Valor inválido para canal_id: {canal_id_str}")
    else:
        logger.info("[DEBUG on_startup] nenhum canal_id_str encontrado")

    # 3) (opcional) configure seus comandos globais
    await setup_commands(app)


main_conv = ConversationHandler(
    entry_points=[
        CommandHandler("admin2", admin),
        CommandHandler("add", add_pontos, filters=filters.ChatType.PRIVATE),
        CommandHandler("del", del_pontos, filters=filters.ChatType.PRIVATE),
        CommandHandler("rem_admin", rem_admin, filters=filters.ChatType.PRIVATE),
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
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)  # <— aqui, não setup_commands
        .build()
    )

    app.add_handler(main_conv)

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
    sort_config_conv = ConversationHandler(
        entry_points=[CommandHandler("configurar_sort", configurar_sort)],
        states={
            CONFIG_SORTEIO_MONTANTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_montante)
            ],
            CONFIG_SORTEIO_PREMIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_valor_premio)
            ],
            CONFIG_SORTEIO_COOLDOWN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_cooldown)
            ],
            CONFIG_SORTEIO_QTD_PARTICIPANTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_qtd_participantes)
            ],
            CONFIG_SORTEIO_TENTATIVAS_POR_USUARIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receber_tentativas_por_usuario)
            ],
            CONFIG_SORTEIO_CONFIRMACAO: [
                CallbackQueryHandler(confirmar_sorteio, pattern="^confirmar_sorteio$"),
                CallbackQueryHandler(cancelar_sort, pattern="^cancelar_sorteio$")
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_sort)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("inicio", cmd_inicio, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('admin', admin, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('meus_pontos', meus_pontos))
    app.add_handler(CommandHandler('historico', historico, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_historico, pattern=r"^hist:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(paginacao_via_start, pattern=r"^via_start:\d+$"))
    app.add_handler(CommandHandler("backup", cmd_backup))
    #app.add_handler(CommandHandler("sortear", sortear))
    app.add_handler(CommandHandler("set", setar_canal))
    app.add_handler(sort_config_conv)

    app.add_handler(CommandHandler('rank_tops', ranking_tops))
    app.add_handler(CommandHandler("historico_usuario", historico_usuario, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("listar_usuarios", listar_usuarios, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_listar_usuarios,pattern=r'^usuarios\|\d+$'))
    app.add_handler(CommandHandler("listar_via_start", listar_via_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("estatisticas", estatisticas, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('como_ganhar', como_ganhar, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("news", news, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("checkin_on", ativar_checkin))
    app.add_handler(CommandHandler("checkin_off", desativar_checkin))
    app.add_handler(CommandHandler("sort_status", sort_status))
    app.add_handler(CommandHandler("cancelar_sort", cancelar_sort))
    app.add_handler(CommandHandler("liberar_ganhadores", liberar_ganhadores, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("list_ganhadores_sort", list_ganhadores_sort))
    app.add_handler(CommandHandler("list_pontuadores",listar_ranking,filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_listar_ranking,pattern=r"^ranking\|\d+$"))

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
