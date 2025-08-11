import re

def extract_code_block(text: str) -> str:
    """
    Extrae el primer bloque de código Python desde una respuesta del modelo.
    Busca triple backticks con o sin lenguaje especificado.

    :param text: Texto completo de la respuesta del agente.
    :return: Código extraído o cadena vacía si no se encuentra.
    """
    # Busca bloques de código entre ``` o ```python
    matches = re.findall(r"```(?:python)?\n(.*?)```", text, re.DOTALL)

    if matches:
        return matches[0].strip()
    else:
        print("[extract_code] No se encontró un bloque de código en la respuesta.")
        return ""
