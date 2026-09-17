"""Diagnóstico del micrófono:  npm run mic:check

Cuando Nova «no te oye» hay al menos cuatro sospechosos —el dispositivo elegido,
el nivel de entrada, la palabra activadora y la transcripción— y desde fuera se
parecen todos. Esto los separa: graba unos segundos de tu micrófono real y dice,
con números, qué pasó en cada etapa.

Deliberadamente no usa nada de la sesión: abre el micrófono por su cuenta. Si la
sesión estuviera rota, este diagnóstico seguiría siendo de fiar.
"""
from __future__ import annotations

import sys
import time

import numpy as np

from .format import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE

# Umbrales orientativos sobre el RMS de la parte hablada. No son mágicos: salen
# de comparar grabaciones que funcionaban con otras que no.
_NIVEL_MUDO = 0.0005
_NIVEL_BAJO = 0.004


def listar_dispositivos() -> None:
    import sounddevice as sd

    print("Dispositivos de entrada disponibles:")
    por_defecto = sd.default.device[0]
    for indice, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        marca = " <- el que usa Nova por defecto" if indice == por_defecto else ""
        print(f"  [{indice}] {dev['name']}{marca}")
    print()


def grabar(segundos: float, device=None) -> np.ndarray:
    import sounddevice as sd

    trozos: list[bytes] = []
    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                           device=device, dtype="int16", channels=1) as stream:
        fin = time.monotonic() + segundos
        while time.monotonic() < fin:
            datos, _ = stream.read(FRAME_SAMPLES)
            trozos.append(bytes(datos))
    return np.frombuffer(b"".join(trozos), dtype=np.int16).astype(np.float32) / 32768.0


def _nivel(audio: np.ndarray) -> tuple[float, float, float]:
    """RMS global, RMS del 20 % más fuerte (la voz) y pico."""
    if audio.size == 0:
        return 0.0, 0.0, 0.0
    ventana = SAMPLE_RATE // 50
    bloques = audio[: audio.size - audio.size % ventana].reshape(-1, ventana)
    rms_bloques = np.sqrt(np.mean(bloques ** 2, axis=1))
    fuertes = np.sort(rms_bloques)[int(len(rms_bloques) * 0.8):]
    return (float(np.sqrt(np.mean(audio ** 2))),
            float(np.mean(fuertes)) if fuertes.size else 0.0,
            float(np.max(np.abs(audio))))


def _diagnostico_nivel(voz: float, pico: float) -> str:
    if voz < _NIVEL_MUDO:
        return ("MUDO. No está entrando audio por ese dispositivo. Elige otro con "
                "--device, o revisa Configuración > Privacidad > Micrófono.")
    if voz < _NIVEL_BAJO:
        return ("MUY BAJO. Sube el volumen de entrada en Windows (Configuración > "
                "Sonido > el micrófono > Volumen) o acércate al micrófono.")
    if pico > 0.95:
        return ("AL BORDE DEL RECORTE. Baja el volumen de entrada en Windows: "
                "la distorsión hace que Whisper confunda nombres propios.")
    return "correcto."


def _probar_wake(audio: np.ndarray, config) -> None:
    from .wakeword import build_wake_word

    engine = build_wake_word(config)
    nombre = type(engine).__name__
    if nombre == "NullWakeWord":
        print("  no hay detector configurado (wake.engine = off)")
        return

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    modelo = getattr(engine, "_model", None)
    historial = getattr(modelo, "prediction_buffer", None)

    def _ultima() -> float:
        """La puntuación de la ventana recién procesada.

        Hay que leerla en cada vuelta y no al final: openWakeWord sólo conserva
        las últimas 30 ventanas, que son 2,4 segundos. Leyendo el historial
        después de una grabación de seis segundos, la palabra activadora dicha al
        principio ya se ha borrado y el máximo sale 0,000 aunque haya sonado
        perfecta. Este diagnóstico lo hacía mal y mentía en la dirección peor:
        decía que el modelo no había oído nada.
        """
        if not historial:
            return 0.0
        return max((v[-1] for v in historial.values() if v), default=0.0)

    detecto = False
    mejor, mejor_t = 0.0, 0.0
    for i in range(0, len(pcm) - FRAME_BYTES, FRAME_BYTES):
        if engine.process(pcm[i:i + FRAME_BYTES]):
            detecto = True
        puntuacion = _ultima()
        if puntuacion > mejor:
            mejor, mejor_t = puntuacion, i / (SAMPLE_RATE * 2)
    engine.close()

    umbral = float(config.get("wake.oww_threshold", 0.5))
    if detecto:
        print(f"  {nombre}: DETECTADA (puntuación máxima {mejor:.3f})")
        return
    print(f"  {nombre}: NO detectada. Puntuación máxima {mejor:.3f} en el "
          f"segundo {mejor_t:.1f} (hace falta {umbral}).")
    if mejor >= umbral * 0.5:
        print(f"    Te quedaste cerca. Baja el umbral en settings.json:")
        print(f'      "wake": {{ "oww_threshold": {max(0.2, mejor * 0.8):.2f} }}')
    elif mejor < 0.01:
        print("    El modelo no reconoce nada parecido. 'hey jarvis' está entrenado")
        print("    con pronunciación inglesa; si lo dices a la española puede que no")
        print("    llegue nunca. Alternativa inmediata, en el .env:")
        print("      NOVA_WAKE_ENGINE=whisper     (reconoce 'Nova', gasta más CPU)")


def _probar_stt(audio: np.ndarray, config) -> None:
    from ..stt.faster_whisper_stt import FasterWhisperStt
    import asyncio

    stt = FasterWhisperStt(
        model=config.get("stt.model", "small"),
        cpu_model=config.get("stt.cpu_model", "tiny"),
        device=config.get("stt.device", "auto"),
        language=config.get("language", "es"),
        vad_filter=bool(config.get("stt.vad_filter", True)),
    )
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()

    async def _run():
        await stt.warmup()
        con = await stt.transcribe(pcm, final=True)
        stt._vad_filter = False
        sin = await stt.transcribe(pcm, final=True)
        await stt.close()
        return con.text, sin.text

    con_vad, sin_vad = asyncio.run(_run())
    print(f"  con filtro de silencio : {con_vad!r}")
    if sin_vad != con_vad:
        # Si sólo se entiende con el filtro apagado, el filtro se está comiendo la
        # voz: pasa con micrófonos flojos o muy ruidosos.
        print(f"  sin filtro de silencio : {sin_vad!r}")
        if not con_vad and sin_vad:
            print("    -> el filtro de silencio se está comiendo tu voz. "
                  "Pon stt.vad_filter = false en settings.json.")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..config import Config

    parser = argparse.ArgumentParser(
        prog="nova-mic", description="Comprueba que Nova te oye.")
    parser.add_argument("--device", default=None,
                        help="índice del dispositivo (ver la lista que sale al arrancar)")
    parser.add_argument("--segundos", type=float, default=6.0)
    parser.add_argument("--solo-listar", action="store_true")
    args = parser.parse_args(argv)

    listar_dispositivos()
    if args.solo_listar:
        return 0

    config = Config.load()
    device = int(args.device) if args.device is not None else config.get("audio.input_device")

    print(f"Grabando {args.segundos:.0f} segundos. Di:  «Hey Jarvis, abre Spotify»")
    print("  3...", end="", flush=True); time.sleep(1)
    print(" 2...", end="", flush=True); time.sleep(1)
    print(" 1...", end="", flush=True); time.sleep(1)
    print(" YA\n")
    audio = grabar(args.segundos, device)

    rms, voz, pico = _nivel(audio)
    print(f"Nivel: RMS {rms:.4f} | voz {voz:.4f} | pico {pico:.3f}")
    print(f"  -> {_diagnostico_nivel(voz, pico)}\n")

    if voz < _NIVEL_MUDO:
        print("Con este nivel no tiene sentido seguir. Arregla el dispositivo primero.")
        return 1

    print("Palabra activadora:")
    _probar_wake(audio, config)
    print("\nTranscripción:")
    _probar_stt(audio, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
