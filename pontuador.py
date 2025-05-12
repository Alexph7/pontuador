import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, Bot
from telegram.ext import (
    Updater, CommandHandler, CallbackContext,
    MessageHandler, filters
)

# --- Configuração e constantes ---
TOKEN = os.getenv('TELEGRAM_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')  # ex: postgres://user:pass@host:port/dbname
ID_ADMIN = 123456789  # ID do administrador do bot
LIMIAR_PONTUADOR = 500  # pontos para status de pontuador
NIVEIS_BRINDES = {1000: 'Brinde Nível 1', 2000: 'Brinde Nível 2'}

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Banco de dados ---

def obter_conexao():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def inicializar_bd():
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            # tabela usuarios
            cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                pontos INTEGER NOT NULL DEFAULT 0,
                nivel_atingido INTEGER NOT NULL DEFAULT 0,
                is_pontuador BOOLEAN NOT NULL DEFAULT FALSE,
                visto BOOLEAN NOT NULL DEFAULT FALSE
            );
            """)
            # tabela historico
            cur.execute("""
            CREATE TABLE IF NOT EXISTS historico_pontos (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES usuarios(user_id),
                pontos INTEGER NOT NULL,
                motivo TEXT,
                data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # tabela bloqueios
            cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios_bloqueados (
                user_id BIGINT PRIMARY KEY,
                motivo TEXT,
                data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # tabela eventos (promoções)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS eventos (
                id SERIAL PRIMARY KEY,
                nome TEXT,
                inicio TIMESTAMP,
                fim TIMESTAMP,
                multiplicador INTEGER DEFAULT 1
            );
            """)
            conn.commit()

# --- Helpers de usuário e pontuação ---

def adicionar_usuario(user_id: int, username: str):
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usuarios (user_id, username) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username;",
                (user_id, username)
            )
            conn.commit()


def obter_usuario(user_id: int):
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE user_id = %s", (user_id,))
            return cur.fetchone()


def registrar_historico(user_id: int, pontos: int, motivo: str = None):
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO historico_pontos (user_id, pontos, motivo) VALUES (%s, %s, %s)",
                (user_id, pontos, motivo)
            )
            conn.commit()


def atualizar_pontos(user_id: int, delta: int, motivo: str = None, bot: Bot = None):
    usuario = obter_usuario(user_id)
    if not usuario:
        return None
    novos = usuario['pontos'] + delta
    ja_pontuador = usuario['is_pontuador']
    nivel = usuario['nivel_atingido']

    # registra histórico
    registrar_historico(user_id, delta, motivo)

    # verifica se vira pontuador
    becomes_pontuador = False
    if not ja_pontuador and novos >= LIMIAR_PONTUADOR:
        ja_pontuador = True
        nivel += 1
        becomes_pontuador = True

    # checa brindes
    brinde = None
    for limiar, nome in NIVEIS_BRINDES.items():
        if usuario['pontos'] < limiar <= novos:
            brinde = nome
            nivel += 1

    # atualiza no banco
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE usuarios SET pontos=%s, is_pontuador=%s, nivel_atingido=%s WHERE user_id=%s",
                (novos, ja_pontuador, nivel, user_id)
            )
            conn.commit()

    # notificações a você (admin)
    if bot:
        admin_id = ID_ADMIN
        if becomes_pontuador:
            bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔔 Usuário `{user_id}` atingiu {novos} pontos e virou PONTUADOR."
                    f"{' Motivo: '+motivo if motivo else ''}"
                ),
                parse_mode='Markdown'
            )
        if brinde:
            bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔔 Usuário `{user_id}` atingiu {limiar} pontos e ganhou *{brinde}*."
                    f"{' Motivo: '+motivo if motivo else ''}"
                ),
                parse_mode='Markdown'
            )

    return novos

# --- Helpers de bloqueio ---

def bloquear_usuario(user_id: int, motivo: str = None):
    """Adiciona um usuário à lista de bloqueados."""
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usuarios_bloqueados (user_id, motivo) VALUES (%s, %s)"
                " ON CONFLICT (user_id) DO UPDATE SET motivo = EXCLUDED.motivo, data = CURRENT_TIMESTAMP;",
                (user_id, motivo)
            )
            conn.commit()


def desbloquear_usuario(user_id: int):
    """Remove um usuário da lista de bloqueados."""
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM usuarios_bloqueados WHERE user_id = %s",
                (user_id,)
            )
            conn.commit()


def obter_bloqueado(user_id: int):
    """Retorna o registro de bloqueio ou None."""
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT motivo FROM usuarios_bloqueados WHERE user_id = %s",
                (user_id,)
            )
            return cur.fetchone()

# --- Middleware de verificação de bloqueio ---

def checar_bloqueio(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    reg = obter_bloqueado(user_id)
    if reg:
        motivo = reg['motivo'] or 'sem motivo especificado'
        # avisa ao usuário bloqueado
        update.message.reply_text(f"⛔ Você está bloqueado. Motivo: {motivo}")
        return True  # sinaliza que a mensagem foi tratada
    return False

# --- Handlers de bloquear e desbloquear ---

def comando_bloquear(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return update.message.reply_text("🔒 Apenas admin pode usar.")
    args = context.args
    if not args:
        return update.message.reply_text("Uso: /bloquear <user_id> [motivo]")
    try:
        alvo = int(args[0])
    except ValueError:
        return update.message.reply_text("ID deve ser um número.")
    motivo = ' '.join(args[1:]) if len(args) > 1 else None
    bloquear_usuario(alvo, motivo)
    update.message.reply_text(f"✅ Usuário {alvo} bloqueado. Motivo: {motivo or 'nenhum'}")


def comando_desbloquear(update: Update, context: CallbackContext):
    if update.effective_user.id != ID_ADMIN:
        return update.message.reply_text("🔒 Apenas admin pode usar.")
    args = context.args
    if not args:
        return update.message.reply_text("Uso: /desbloquear <user_id>")
    try:
        alvo = int(args[0])
    except ValueError:
        return update.message.reply_text("ID deve ser um número.")
    desbloquear_usuario(alvo)
    update.message.reply_text(f"✅ Usuário {alvo} desbloqueado.")


# --- Handlers de comando ---

def comando_iniciar(update: Update, context: CallbackContext):
    user = update.effective_user
    adicionar_usuario(user.id, user.username)
    update.message.reply_text("Olá! Sou o bot pontuador. Use /meus_pontos para ver seus pontos.")


def meus_pontos(update: Update, context: CallbackContext):
    user = update.effective_user
    adicionar_usuario(user.id, user.username)
    u = obter_usuario(user.id)
    update.message.reply_text(f"Você tem {u['pontos']} pontos (Nível {u['nivel_atingido']}).")


def comando_pontuar(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = obter_usuario(chamador)
    if not reg['is_pontuador']:
        return update.message.reply_text("🔒 Sem permissão para pontuar.")

    args = context.args
    if len(args) < 2:
        return update.message.reply_text("Uso: /pontuar <user_id> <pontos> [motivo]")

    try:
        alvo_id = int(args[0]); pts = int(args[1])
    except ValueError:
        return update.message.reply_text("IDs e pontos devem ser números.")

    motivo = ' '.join(args[2:]) if len(args)>2 else None
    if not obter_usuario(alvo_id):
        return update.message.reply_text("Usuário não encontrado.")

    novos = atualizar_pontos(alvo_id, pts, motivo, context.bot)
    update.message.reply_text(f"✅ Atribuídos {pts} pontos ao usuário {alvo_id}.")


def comando_adicionar_pontuador(update: Update, context: CallbackContext):
    chamador = update.effective_user.id
    reg = obter_usuario(chamador)
    if chamador!=ID_ADMIN and not reg['is_pontuador']:
        return update.message.reply_text("🔒 Sem permissão.")
    try:
        novo = int(context.args[0])
    except:
        return update.message.reply_text("Uso: /add_pontuador <user_id>")
    adicionar_usuario(novo, None)
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET is_pontuador=TRUE WHERE user_id=%s",(novo,))
            conn.commit()
    update.message.reply_text(f"✅ Usuário {novo} virou pontuador.")


def comando_historico(update: Update, context: CallbackContext):
    user = update.effective_user
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data,pontos,motivo FROM historico_pontos WHERE user_id=%s ORDER BY data DESC LIMIT 10",
                (user.id,)
            )
            rows = cur.fetchall()
    lines = [f"{r['data'].strftime('%d/%m %H:%M')}: {r['pontos']} pts - {r['motivo']}" for r in rows]
    update.message.reply_text("🗒️ Seu histórico:\n" + "\n".join(lines))


def comando_ranking_top10(update: Update, context: CallbackContext):
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username,pontos FROM usuarios ORDER BY pontos DESC LIMIT 10")
            top = cur.fetchall()
    text = "🏅 Top 10 de pontos:\n" + "\n".join([f"{i+1}. {u['username']} – {u['pontos']} pts" for i,u in enumerate(top)])
    update.message.reply_text(text)


def comando_zerar_pontos(update: Update, context: CallbackContext):
    if update.effective_user.id!=ID_ADMIN:
        return update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo=int(context.args[0])
    except:
        return update.message.reply_text("Uso: /zerar_pontos <user_id>")
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET pontos=0 WHERE user_id=%s",(alvo,))
            conn.commit()
    update.message.reply_text(f"✅ Pontos de {alvo} zerados.")


def comando_remover_pontuador(update: Update, context: CallbackContext):
    if update.effective_user.id!=ID_ADMIN:
        return update.message.reply_text("🔒 Apenas admin pode usar.")
    try:
        alvo=int(context.args[0])
    except:
        return update.message.reply_text("Uso: /remover_pontuador <user_id>")
    with obter_conexao() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET is_pontuador=FALSE WHERE user_id=%s",(alvo,))
            conn.commit()
    update.message.reply_text(f"✅ {alvo} não é mais pontuador.")

# --- Handler de presença ---

def tratar_presenca(update: Update, context: CallbackContext):
    user = update.effective_user
    adicionar_usuario(user.id, user.username)
    reg = obter_usuario(user.id)
    if not reg['visto']:
        atualizar_pontos(user.id, 1, 'Presença inicial', context.bot)
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE usuarios SET visto=TRUE WHERE user_id=%s",(user.id,))
                conn.commit()
        update.message.reply_text("👋 +1 ponto por participar!")

def como_ganhar(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🎯 Você ganha pontos por:\n"
        "• Presença inicial (/start ou primeira mensagem no grupo)\n"
        "• Ser pontuador (500 pts)\n"
        "• Receber pontuações de outros\n"
        "\nUse /meus_pontos para ver seu total!"
    )

# --- Inicialização do bot ---

if __name__ == '__main__':
    inicializar_bd()
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(filters.all, checar_bloqueio), group=0)

    # Comandos básicos
    dp.add_handler(CommandHandler('start', comando_iniciar))
    dp.add_handler(CommandHandler('meus_pontos', meus_pontos))
    dp.add_handler(CommandHandler('como_ganhar', como_ganhar))

    # Administração e pontuação
    dp.add_handler(CommandHandler('pontuar', comando_pontuar))
    dp.add_handler(CommandHandler('add_pontuador', comando_adicionar_pontuador))
    dp.add_handler(CommandHandler('historico', comando_historico))
    dp.add_handler(CommandHandler('ranking_top10', comando_ranking_top10))
    dp.add_handler(CommandHandler('zerar_pontos', comando_zerar_pontos))
    dp.add_handler(CommandHandler('remover_pontuador', comando_remover_pontuador))
    dp.add_handler(CommandHandler('bloquear', comando_bloquear))
    dp.add_handler(CommandHandler('desbloquear', comando_desbloquear))

    # Presença
    dp.add_handler(MessageHandler(filters.all & filters.chat_type.groups, tratar_presenca))

    updater.start_polling()
    updater.idle()
