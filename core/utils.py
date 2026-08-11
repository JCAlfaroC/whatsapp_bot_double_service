# --- core/utils.py ---
"""Utilidades de texto, fechas y menús que usan los dos flujos.

Las dos versiones originales de estas funciones eran casi idénticas; aquí queda
una sola. Las diferencias que había se resolvieron a favor de la versión más
tolerante, porque ninguna de las dos se rompe con ella (ver normalize_text).
"""

import unicodedata
from datetime import datetime

from thefuzz import process

DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def normalize_text(text):
    """Deja el texto comparable: sin mayúsculas, tildes, puntuación ni espacios
    de más ('¿Sábado?' -> 'sabado').

    Se usa para reconocer lo que escribe el usuario, que rara vez viene
    acentuado desde el teclado del teléfono.

    El descarte de ' s a c' / ' sac' viene del bot de pacientes, donde las sedes
    llegan con la razón social ('... S.A.C.') y el paciente nunca la escribe. Se
    conserva para el flujo de quirófanos también: allí no hay ningún nombre ni
    comando que contenga ese fragmento, así que no cambia nada.
    """
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    text = text.replace(".", "").replace(",", "").replace("-", " ")
    text = text.replace(" s a c", "").replace(" sac", "")
    return " ".join(text.split())


def format_date_es(date_obj):
    return f"{DAYS_ES[date_obj.weekday()]}, {date_obj.day:02d} de {MONTHS_ES[date_obj.month]}"


def format_duration_es(hours):
    """1 -> '1 hora'; 1.5 -> '1,5 horas'; 2 -> '2 horas'.

    Con bloques de 30 minutos la duración puede caer en media hora, así que se
    usa ':g' (que no deja el '.0' de las duraciones enteras) y coma decimal, que
    es como se escribe en Perú.
    """
    label = f"{hours:g}".replace(".", ",")
    return f"{label} hora" + ("s" if hours != 1 else "")


def process_user_choice(user_input, options, key_name=None):
    """Traduce lo que respondió el usuario a una de las opciones que se le
    mostraron.

    El camino normal es el número de la lista. 'key_name' habilita además
    reconocer la opción por su nombre escrito (exacto o aproximado, con un
    mínimo de parecido para no adivinar mal por él); las pantallas que no lo
    pasan aceptan sólo el número.
    """
    try:
        choice_index = int(user_input) - 1
        if 0 <= choice_index < len(options):
            return options[choice_index]["data"]
    except (ValueError, IndexError):
        if not key_name:
            return None
        normalized_input = normalize_text(user_input)
        for opt in options:
            if normalize_text(opt["data"].get(key_name, "")) == normalized_input:
                return opt["data"]
        option_names = [opt["data"].get(key_name, "") for opt in options]
        if not option_names:
            return None
        best_match, score = process.extractOne(user_input, option_names)
        if score > 75:
            for opt in options:
                if opt["data"].get(key_name, "") == best_match:
                    return opt["data"]
    return None


def resolve_selection(message_text, selected_id, session):
    """Resuelve la opción elegida, venga de un tap en una lista interactiva
    (selected_id) o del número que escribió el usuario.

    Se prueba primero el id porque es exacto; el número se resuelve por posición
    sobre las mismas 'options' que se acaban de mostrar.
    """
    options = session.get("options", [])
    if selected_id:
        for opt in options:
            if opt["data"].get("_id") == selected_id:
                return opt["data"]
    return process_user_choice(message_text, options)


def format_menu(title, items, key_id, key_name, key_price=None):
    """Menú numerado en texto plano + las 'options' que lo interpretan.

    Es el render del flujo de pacientes: todas sus pantallas se responden con el
    número de la lista, y el texto plano entra siempre completo, sin el límite
    de filas de las listas interactivas de WhatsApp.
    """
    menu_text = f"{title}\n\n"
    formatted_items = []
    for i, item in enumerate(items, 1):
        display_name = item.get(key_name, "")
        if key_id == "citdat":
            try:
                date_obj = datetime.strptime(item.get(key_id, ""), "%Y%m%d")
                display_name = format_date_es(date_obj)
            except (ValueError, TypeError):
                display_name = item.get(key_id, "Fecha inválida")
        if key_price and item.get(key_price) is not None:
            display_name = f"{display_name} – S/ {item[key_price]:.2f}"
        menu_text += f"*{i}.* {display_name}\n"
        formatted_items.append({"id": i, "data": item})
    menu_text += (
        "\n_Escribe el número o el nombre de tu elección._"
        "\n_También puedes escribir *'retroceder'* o *'salir'*._"
    )
    return menu_text, formatted_items
