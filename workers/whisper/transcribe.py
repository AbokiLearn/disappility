# modified from https://github.com/davabase/whisper_real_time

from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
import speech_recognition as sr
import numpy as np
import sounddevice  # noqa: F401
import torch

from sys import platform
from pathlib import Path
from queue import Queue
from time import sleep
from enum import Enum
import datetime as dt
import tempfile
import argparse
import re

ptn = re.compile("([h]?an[n]?a)(.*?)(thank(?:s| you))")

MODEL_CHOICES = {
    "small": "distil-whisper/distil-small.en",
    "medium": "distil-whisper/distil-medium.en",
    "large": "distil-whisper/distil-large-v3",
}

DEFAULT_MODEL = "small"


class msg(str, Enum):
    READY = "[READY]"
    USERSAYS = "[USERSAYS]"


def get_tempfile() -> Path:
    suffix = f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}-aiccessible.transcript"
    return Path(tempfile.mktemp(suffix=suffix))


def save_transcripts(transcripts: list[str]) -> None:
    with open(get_tempfile(), "w") as f:
        f.write("\n".join(transcripts))


def get_cmd(transcript: str, ptn: re.Pattern) -> str:
    cmd = re.sub("[.,;:!?]", " ", transcript).strip()
    match = ptn.search(cmd)
    if match:
        match = [x.strip() for x in match.groups()]
        return match[1]
    else:
        return None


def load_model(model_name: str, use_cpu: bool = False):
    try:
        if use_cpu:
            device = torch.device("cpu")
            dtype = torch.float32
        else:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            dtype = torch.float16 if torch.cuda.is_available() else torch.float32

        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_name,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
        )
        model.to(device)

        processor = AutoProcessor.from_pretrained(model_name)

        pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            max_new_tokens=128,
            torch_dtype=dtype,
            device=device,
        )

        return pipe
    except Exception as e:
        print(f"Error loading model {model_name}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model to use",
        choices=MODEL_CHOICES.keys(),
    )
    parser.add_argument(
        "--energy_threshold",
        default=1000,
        help="Energy level for mic to detect.",
        type=int,
    )
    parser.add_argument(
        "--record_timeout",
        default=2,
        help="How real time the recording is in seconds.",
        type=float,
    )
    parser.add_argument(
        "--phrase_timeout",
        default=4,
        help="How much empty space between recordings before we "
        "consider it a new line in the transcription.",
        type=float,
    )
    parser.add_argument(
        "--use_cpu",
        action="store_true",
        default=True,
        help="Set this flag to force the model to use CPU instead of GPU.",
    )
    if "linux" in platform:
        parser.add_argument(
            "--default_microphone",
            default="pulse",
            help="Default microphone name for SpeechRecognition. "
            "Run this with 'list' to view available Microphones.",
            type=str,
        )
    args = parser.parse_args()

    # The last time a recording was retrieved from the queue.
    phrase_time = None
    # Thread safe Queue for passing data from the threaded recording callback.
    data_queue = Queue()
    # We use SpeechRecognizer to record our audio because it has a nice feature where it can detect when speech ends.
    recorder = sr.Recognizer()
    recorder.energy_threshold = args.energy_threshold
    # Definitely do this, dynamic energy compensation lowers the energy threshold dramatically to a point where the SpeechRecognizer never stops recording.
    recorder.dynamic_energy_threshold = False

    # Important for linux users.
    # Prevents permanent application hang and crash by using the wrong Microphone
    if "linux" in platform:
        mic_name = args.default_microphone
        if not mic_name or mic_name == "list":
            print("Available microphone devices are: ")
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                print(f'Microphone with name "{name}" found')
            return
        else:
            for index, name in enumerate(sr.Microphone.list_microphone_names()):
                if mic_name in name:
                    source = sr.Microphone(sample_rate=16000, device_index=index)
                    break
    else:
        source = sr.Microphone(sample_rate=16000)

    # Load / Download model
    model = MODEL_CHOICES[args.model]
    audio_model = load_model(model, use_cpu=args.use_cpu)

    record_timeout = args.record_timeout
    phrase_timeout = args.phrase_timeout

    transcription = [""]
    transcript = ""

    with source:
        recorder.adjust_for_ambient_noise(source)

    def record_callback(_, audio: sr.AudioData) -> None:
        """
        Threaded callback function to receive audio data when recordings finish.
        audio: An AudioData containing the recorded bytes.
        """
        # Grab the raw bytes and push it into the thread safe queue.
        data = audio.get_raw_data()
        data_queue.put(data)

    # Create a background thread that will pass us raw audio bytes.
    # We could do this manually but SpeechRecognizer provides a nice helper.
    recorder.listen_in_background(
        source, record_callback, phrase_time_limit=record_timeout
    )

    # Cue the user that we're ready to go.
    print(msg.READY.value, flush=True)

    while True:
        try:
            now = dt.datetime.utcnow()
            # Pull raw recorded audio from the queue.
            if not data_queue.empty():
                phrase_complete = False
                # If enough time has passed between recordings, consider the phrase complete.
                # Clear the current working audio buffer to start over with the new data.
                if phrase_time and now - phrase_time > dt.timedelta(
                    seconds=phrase_timeout
                ):
                    phrase_complete = True
                # This is the last time we received new audio data from the queue.
                phrase_time = now

                # Combine audio data from queue
                audio_data = b"".join(data_queue.queue)
                data_queue.queue.clear()

                # Convert in-ram buffer to something the model can use directly without needing a temp file.
                # Convert data from 16 bit wide integers to floating point with a width of 32 bits.
                # Clamp the audio stream frequency to a PCM wavelength compatible default of 32768hz max.
                audio_np = (
                    np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                    / 32768.0
                )

                # Read the transcription.
                result = audio_model(audio_np)
                text = result["text"].strip()

                # If we detected a pause between recordings, add a new item to our transcription.
                # Otherwise edit the existing one.
                if phrase_complete:
                    transcription.append(text)
                else:
                    transcription[-1] += text
                transcript += text + " "

                match = ptn.search(transcript.lower())
                if match:
                    cmd = get_cmd(transcript.lower(), ptn)
                    print(f"{msg.USERSAYS} {cmd}", flush=True)
                    transcript = ""

            else:
                # Infinite loops are bad for processors, must sleep.
                sleep(0.25)

        except KeyboardInterrupt:
            break

    print("transcription:")
    for line in transcription:
        print(line)
    save_transcripts(transcript)


if __name__ == "__main__":
    main()
