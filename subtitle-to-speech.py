import io
import re
import datetime
import concurrent.futures
from pathlib import Path
import srt
import openai
from pydub import AudioSegment
import streamlit as st
from utils import INSTRUCTIONS

client = openai.OpenAI()

MAX_WORKERS = 20  # Max threads for TTS synthesis

_SBv_TIME_RE = re.compile(
    r"^\s*(\d+:)?\d{1,2}:\d{2}\.\d{1,3}\s*,\s*(\d+:)?\d{1,2}:\d{2}\.\d{1,3}\s*$"
)


def _str_to_timedelta(ts: str) -> datetime.timedelta:
    """
    Converts an SBV timestamp string to a datetime.timedelta object. 
    
    This function parses a timestamp string in the SBV subtitle format (e.g., "0:01:02.500" or "01:02.500")
    and returns the corresponding timedelta object representing the duration.

    Args:
        ts (str): The SBV timestamp string to convert. It can be in the format "H:MM:SS.mmm" or "MM:SS.mmm".

    Returns:
        datetime.timedelta: The duration represented by the input timestamp.
    """
    h, m, s_ms = 0, 0, ts
    if ts.count(":") == 2:  # hours present
        h, m, s_ms = ts.split(":", 2)
    else:  # minutes only
        m, s_ms = ts.split(":", 1)
    sec, ms = s_ms.split(".")
    return datetime.timedelta(
        hours=int(h), minutes=int(m), seconds=int(sec), milliseconds=int(ms)
    )


def parse_sbv(text: str):
    """
    Parses SBV (SubViewer) subtitle text and returns a list of srt.Subtitle objects.

    The SBV format consists of blocks with a start and end timestamp on the first line,
    followed by one or more lines of caption text, separated by blank lines.

    Args:
        text (str): The SBV-formatted subtitle text to parse.

    Returns:
        list[srt.Subtitle] or None: A list of srt.Subtitle objects parsed from the input text,
        or None if parsing fails or no subtitles are found.
    """
    subtitles, idx = [], 1
    lines = iter(text.splitlines())
    try:
        for line in lines:
            if not line.strip():
                continue  # Skip empty separators.
            if not _SBv_TIME_RE.match(line):
                # Try skipping potential header lines in some SBV variations.
                continue

            start_str, end_str = [t.strip() for t in line.split(",", 1)]
            caption_lines = []
            for caption_line in lines:  # Gather text until blank line.
                if not caption_line.strip():
                    break
                caption_lines.append(caption_line)
            content = " ".join(caption_lines)
            subtitles.append(
                srt.Subtitle(
                    index=idx,
                    start=_str_to_timedelta(start_str),
                    end=_str_to_timedelta(end_str),
                    content=content,
                )
            )
            idx += 1
    except Exception as e:
        st.error(f"Fehler beim Parsen des SBV-Inhalts: {e}")
        st.text("Die problematische Zeile könnte hier sein:")
        st.code(line)
        return None
    if not subtitles:
        st.warning(
            "Ich konnte keine Untertitel aus dem SBV-Inhalt extrahieren. Ist das Format korrekt?"
        )
        return None
    return subtitles


def read_subtitles(file_content: str, filename: str):
    """
    Parses subtitle file content and returns a list of Subtitle objects, supporting both SRT and SBV formats.

    Args:
        file_content (str): The content of the subtitle file as a string.
        filename (str): The name of the subtitle file, used to determine the file extension.

    Returns:
        list[srt.Subtitle] or None: A list of Subtitle objects parsed from the file, or None if parsing fails.
    """
    file_suffix = Path(filename).suffix.lower()
    try:
        if file_suffix == ".sbv" or _SBv_TIME_RE.match(file_content.splitlines()[0]):
            st.info("SBV-Format erkannt.")
            return parse_sbv(file_content)
        else:
            st.info("SRT-Format erkannt.")
            return list(srt.parse(file_content))
    except Exception as e:
        st.error(f"Fehler beim Parsen der Untertiteldatei '{filename}': {e}")
        return None


def match_target_amplitude(sound: AudioSegment, target_dbfs: float) -> AudioSegment:
    """
    Adjusts the amplitude of an AudioSegment to match a target dBFS (decibels relative to full scale).

    This function computes the difference between the current loudness of the input audio and the desired target dBFS,
    then applies the necessary gain to achieve the target loudness.

    Args:
        sound (AudioSegment): The input audio segment whose amplitude is to be adjusted.
        target_dbfs (float): The desired loudness in dBFS.

    Returns:
        AudioSegment: A new AudioSegment instance with its amplitude adjusted to the target dBFS.
    """
    change = target_dbfs - sound.dBFS
    return sound.apply_gain(change)


def tts_segment(text: str, model: str = "gpt-4o-mini-tts", voice: str = "alloy", fmt: str = "wav") -> AudioSegment | None:
    """
    Synthesizes a text segment into speech using OpenAI's TTS API and returns it as an AudioSegment.

    Args:
        text (str): The text to synthesize into speech.
        model (str): The TTS model to use (default: "gpt-4o-mini-tts").
        voice (str): The voice to use for synthesis (default: "alloy").
        fmt (str): The audio format for the output (default: "wav").

    Returns:
        AudioSegment: The synthesized speech as an AudioSegment object, or None if synthesis fails.
    """
    
    resp = client.audio.speech.create(
        model=model,
        voice=voice,
        input=text,
        instructions=INSTRUCTIONS,
        response_format=fmt,
    )
    audio_bytes = resp.content
    return AudioSegment.from_file(io.BytesIO(audio_bytes), format=fmt)


def synthesize(
    subs: list,
    voice="alloy",
    model="gpt-4o-mini-tts",
    fmt="wav",
    pad_ms=50,
    normalize=True,
    target_db=-20.0,
    max_workers=MAX_WORKERS,
) -> io.BytesIO | None:
    """
    Synthesizes audio from subtitles concurrently and returns it as an in-memory BytesIO object.
    
    Args:
        subs (list): A list of srt.Subtitle objects to synthesize.
        voice (str): The voice to use for synthesis (default: "alloy").
        model (str): The TTS model to use (default: "gpt-4o-mini-tts").
        fmt (str): The audio format for the output (default: "wav").
        pad_ms (int): Duration of silence padding between segments in milliseconds (default: 50).
        normalize (bool): Whether to normalize the audio segments (default: True).
        target_db (float): Target loudness in dBFS for normalization (default: -20.0).
        max_workers (int): Maximum number of parallel threads for TTS synthesis (default: MAX_WORKERS).

    Returns:
        io.BytesIO: An in-memory BytesIO object containing the synthesized audio, or None if synthesis fails.
    """
    if not subs:
        st.error("Keine Untertitel zum Synthetisieren bereitgestellt.")
        return None

    track = AudioSegment.silent(0)
    total_subs = len(subs)
    progress_bar = st.progress(0, text="Starte Audio-Synthese...")
    results = [None] * total_subs
    futures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, sub in enumerate(subs):
            clean_content = sub.content.replace("\n", " ").strip()
            if not clean_content:
                st.warning(f"Überspringe leeren Untertitel bei Index {i + 1}")
                # Store placeholder for empty segment to maintain order.
                results[i] = (sub.start, AudioSegment.silent(0))
                continue  # Skip empty subtitles.

            future = executor.submit(
                tts_segment, clean_content, model=model, voice=voice, fmt=fmt
            )
            futures.append((i, sub.start, future))

        # Process completed futures
        completed_count = 0
        for i, start_time, future in futures:
            try:
                speech = future.result()
                if speech is None:
                    st.error(
                        f"Fehler beim Synthetisieren von Segment {i + 1}. Füge Stille ein."
                    )
                    # TODO: Estimate duration based on previous/next or average?
                    # For now, add 0 silence.
                    speech = AudioSegment.silent(0)
                elif normalize:
                    try:
                        speech = match_target_amplitude(speech, target_db)
                    except Exception as e:
                        st.warning(f"Konnte Segment {i + 1} nicht normalisieren: {e}")
                results[i] = (start_time, speech)  # Store result with start time.
            except Exception as exc:
                st.error(f"Segment {i + 1} generierte einen Fehler: {exc}")
                results[i] = (start_time, AudioSegment.silent(0))

            finally:
                completed_count += 1
                progress_text = (
                    f"Verarbeite Segment {completed_count}/{len(futures)}..."
                )
                progress_bar.progress(
                    completed_count / len(futures), text=progress_text
                )

    # Assemble the track in the correct order, handling timing and padding.
    progress_bar.progress(1.0, text="Füge Audio-Segmente zusammen...")
    cursor = 0
    # Sort results by original index to ensure correct order before processing timing
    # The futures list was already ordered by submission, which matches subtitle order.
    # We stored results in the `results` list using the original index `i`.
    for i in range(total_subs):
        if results[i] is None:  # Handle skipped empty subtitles.
            continue

        start_td, speech_segment = results[i]
        start_ms = int(start_td.total_seconds() * 1000)

        if start_ms > cursor:
            # Add silence gap if needed.
            track += AudioSegment.silent(start_ms - cursor)
            cursor = start_ms
        elif start_ms < cursor:
            # Overlap detected.
            st.warning(
                f"Untertitel {i + 1} Startzeit ({start_td}) überlappt mit vorherigem Segmentende. Anpassung erfolgt."
            )
            # Option 1: Truncate the track to the required start time.
            # track = track[:start_ms]
            # Option 2: Overwrite (default pydub behavior with +)
            # - let's stick with this for now.
            cursor = start_ms  # Reset cursor to the required start time.

        track += speech_segment
        cursor += len(speech_segment)

        # Add padding after the speech segment.
        track += AudioSegment.silent(pad_ms)
        cursor += pad_ms

    progress_bar.progress(1.0, text="Synthese abgeschlossen. Exportiere...")

    # Export to an in-memory bytes buffer.
    try:
        output_buffer = io.BytesIO()
        track.export(output_buffer, format=fmt)
        output_buffer.seek(0)  # Rewind buffer to the beginning.
        progress_bar.empty()  # Clear progress bar.
        st.success("Audio erfolgreich generiert!")
        return output_buffer
    except Exception as e:
        st.error(f"Fehler beim Exportieren der Audiospur: {e}")
        progress_bar.empty()
        return None


# --- Streamlit App UI ---

st.set_page_config(page_title="SRT2Audio", layout="wide")
st.title("🎙️ Untertitel-zu-Sprache-Konverter")
st.markdown(
    "Lade eine SRT- oder SBV-Untertiteldatei hoch und wähle eine Stimme und ein Audioformat. Diese App generiert dir gesprochenes Audio mit einer synthetischen Stimme von [OpenAI](https://www.openai.fm/). :red[**Wichtig: Deine Inhalte werden bei Clouddiensten verarbeitet (OpenAI). Verwende daher nur nicht sensitive, öffentliche Daten.**]"
)

# --- Sidebar Settings ---
with st.sidebar:
    st.header("Audio-Einstellungen")
    selected_voice = st.selectbox(
        "Stimme auswählen",
        options=["alloy", "echo", "fable", "nova", "shimmer"],
        index=4,
        help="Wähle die Stimme, die für die Sprachausgabe verwendet werden soll.",
    )
    selected_format = st.selectbox(
        "Ausgabeformat",
        options=["mp3", "wav", "opus", "aac", "flac"],
        index=0,
        help="Wähle das gewünschte Audioformat für die Ausgabe.",
    )
    normalize = st.checkbox(
        "Audio-Segmente normalisieren",
        value=True,
        help="Aktiviere diese Option, um die Lautstärke der Audio-Segmente auf ein einheitliches Niveau zu bringen.",
    )
    target_db_input = st.number_input(
        "Ziellautstärke (dBFS)",
        value=-14.0,
        step=1.0,
        disabled=not normalize,
        help="Bestimme hier die Lautstärke der Audioausgabe. Ein Wert von -14 dBFS ist ein guter Ausgangspunkt.",
    )
    with st.expander("Erweiterte Optionen", expanded=False):
        selected_model = st.selectbox(
            "Modell auswählen",
            options=[
                "gpt-4o-mini-tts",
                "tts-1",
                "tts-1-hd",
            ],
            index=0,
            help="gpt-4o-mini-tts bietet die beste Qualität und Steuerbarkeit.",
        )
        pad_ms = st.slider(
            "Stille zwischen Segmenten (ms)",
            min_value=0,
            max_value=1000,
            value=50,
            step=10,
            help="Wähle die Dauer der Stille zwischen den Audio-Segmenten in Millisekunden.",
        )
        max_workers_input = st.number_input(
            "Max. parallele Anfragen (Threads)",
            min_value=1,
            max_value=50,
            value=MAX_WORKERS,
            step=1,
            help="Anzahl der parallelen Anfragen an die Schnittstelle (API) für die gleichzeitige Audio-Synthese. Höhere Werte beschleunigen die Verarbeitung, erhöhen aber auch das Risiko von API-Ratenlimits.",
        )

# --- File Upload ---
st.header("1. Untertiteldatei hochladen")
uploaded_file = st.file_uploader("Wähle eine SRT- oder SBV-Datei", type=["srt", "sbv"])

# --- Processing and Download ---
if uploaded_file is not None:
    st.header("2. Audio generieren")
    # To read file as string:
    string_data = uploaded_file.getvalue().decode(
        "utf-8-sig"
    )  # Use utf-8-sig to handle potential BOM.

    # Display file content preview (optional).
    with st.expander("Vorschau Untertitelinhalt"):
        st.text(string_data[:500] + "...")  # Show first 500 chars.

    # Parse subtitles
    subs = read_subtitles(string_data, uploaded_file.name)

    if subs:
        st.success(f"Erfolgreich {len(subs)} Untertitelsegmente eingelesen.")

        if st.button(f"{selected_format.upper()} Audio generieren", type="primary"):
            with st.spinner("Generiere Audio... Dies kann eine Weile dauern."):
                # Pass max_workers from the input widget.
                audio_buffer = synthesize(
                    subs,
                    voice=selected_voice,
                    model=selected_model,
                    fmt=selected_format,
                    pad_ms=pad_ms,
                    normalize=normalize,
                    target_db=target_db_input,
                    max_workers=max_workers_input,
                )

            if audio_buffer:
                st.header("3. Audio herunterladen")
                output_filename = (
                    f"{Path(uploaded_file.name).stem}__{selected_voice}."
                    f"{selected_format}"
                )
                st.download_button(
                    label=f"{output_filename} herunterladen",
                    data=audio_buffer,
                    file_name=output_filename,
                    mime=f"audio/{selected_format}",
                )
                # Optionally offer to play the audio directly in Streamlit.
                st.audio(audio_buffer, format=f"audio/{selected_format}")

    else:
        st.error(
            "Konnte keine Untertitel aus der hochgeladenen Datei parsen. Bitte überprüfe das Dateiformat und den Inhalt."
        )

else:
    st.info("Lade eine Untertiteldatei hoch, um zu beginnen.")

# --- Footer / Info ---
st.markdown("---")
st.markdown(
    "Ein Prototyp vom Statistischen Amt, Team Data in Zusammenarbeit mit dem Team Informationszugang & Dialog, Staatskanzlei, Kanton Zürich."
)
