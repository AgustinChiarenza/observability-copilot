"""Detectores: lo que corre solo y avisa.

Un detector lee por los mismos puertos que el agente, decide con un cálculo que
está en código —no en un prompt— y entrega un `Message` al despachante. El LLM
no participa: un número que dispara un SMS a las 4 de la mañana se calcula de
forma determinista, se testea, y se puede explicar en una línea.

Hoy hay uno: el pico de gasto. Los análisis declarativos en YAML son de F5.
"""
