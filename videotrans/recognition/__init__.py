# 语音识别
import json
import os
import re
import shutil
import threading
import time
from datetime import timedelta
import torch
from faster_whisper import WhisperModel
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

from videotrans.configure import config
from videotrans.util import tools
import logging

logging.basicConfig()
logging.getLogger("faster_whisper").setLevel(logging.DEBUG)


# 统一入口
def run(*, type="all", detect_language=None, audio_file=None, cache_folder=None, model_name=None, set_p=True, inst=None,
        model_type='faster', is_cuda=None):
    if config.exit_soft :
        return False
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    if model_name.startswith('distil-'):
        model_name = model_name.replace('-whisper', '')
    if model_type == 'openai':
        rs = split_recogn_openai(detect_language=detect_language, audio_file=audio_file, cache_folder=cache_folder,
                                 model_name=model_name, set_p=set_p, inst=inst, is_cuda=is_cuda)
    elif model_type == 'GoogleSpeech':
        rs = google_recogn(detect_language=detect_language, audio_file=audio_file, cache_folder=cache_folder,
                           set_p=set_p, inst=None)
    elif type == "all":
        rs = all_recogn(detect_language=detect_language, audio_file=audio_file, cache_folder=cache_folder,
                        model_name=model_name, set_p=set_p, inst=inst, is_cuda=is_cuda)
    elif type == 'avg' or os.path.exists(config.rootdir + "/old.txt"):
        rs = split_recogn_old(detect_language=detect_language, audio_file=audio_file, cache_folder=cache_folder,
                              model_name=model_name, set_p=set_p, inst=inst, is_cuda=is_cuda)
    else:
        rs = split_recogn(detect_language=detect_language, audio_file=audio_file, cache_folder=cache_folder,
                          model_name=model_name, set_p=set_p, inst=inst, is_cuda=is_cuda)
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return rs


# 整体识别，全部传给模型
def all_recogn(*, detect_language=None, audio_file=None, cache_folder=None, model_name="base", set_p=True, inst=None, is_cuda=None):
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    if set_p:
        tools.set_process(f"{config.params['whisper_model']} {config.transobj['kaishishibie']}",btnkey=inst.btnkey if inst else "")
    down_root = os.path.normpath(config.rootdir + "/models")
    model = None
    try:
        model = WhisperModel(model_name, device="cuda" if is_cuda else "cpu",
                             compute_type="float32" if model_name.startswith('distil-') else config.settings[
                                 'cuda_com_type'],
                             download_root=down_root,
                             num_workers=config.settings['whisper_worker'],
                             cpu_threads=os.cpu_count() if int(config.settings['whisper_threads']) < 1 else int(
                                 config.settings['whisper_threads']),
                             local_files_only=True)
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            return False
        if not tools.vail_file(audio_file):
            raise Exception(f'[error]not exists {audio_file}')
        segments, info = model.transcribe(audio_file,
                                          beam_size=config.settings['beam_size'],
                                          best_of=config.settings['best_of'],
                                          condition_on_previous_text=config.settings['condition_on_previous_text'],

                                          temperature=0 if config.settings['temperature'] == 0 else [0.0, 0.2, 0.4, 0.6,
                                                                                                     0.8, 1.0],
                                          vad_filter=bool(config.settings['vad']),
                                          vad_parameters=dict(
                                              min_silence_duration_ms=config.settings['overall_silence'],
                                              max_speech_duration_s=config.settings['overall_maxsecs']
                                          ),
                                          word_timestamps=True,
                                          language=detect_language,
                                          initial_prompt=None if detect_language != 'zh' else config.settings[
                                              'initial_prompt_zh'])

        # 保留原始语言的字幕
        raw_subtitles = []
        sidx = -1
        for segment in segments:
            if config.exit_soft :
                del model
                return False
            if config.current_status != 'ing' and config.box_recogn != 'ing':
                del model
                return None
            sidx += 1
            start = int(segment.words[0].start * 1000)
            end = int(segment.words[-1].end * 1000)
            # if start == end:
            #     end += 200
            startTime = tools.ms_to_time_string(ms=start)
            endTime = tools.ms_to_time_string(ms=end)
            text = segment.text.strip().replace('&#39;', "'")
            if detect_language == 'zh' and text == config.settings['initial_prompt_zh']:
                continue
            text = re.sub(r'&#\d+;', '', text)
            # 无有效字符
            if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text) or len(text) <= 1:
                continue
            # 原语言字幕
            s = {"line": len(raw_subtitles) + 1, "time": f"{startTime} --> {endTime}", "text": text}
            raw_subtitles.append(s)
            if set_p:
                tools.set_process(f'{s["line"]}\n{startTime} --> {endTime}\n{text}\n\n', 'subtitle')
                if inst and inst.precent < 55:
                    inst.precent += round(segment.end * 0.5 / info.duration, 2)
                tools.set_process(f'{config.transobj["zimuhangshu"]} {s["line"]}',btnkey=inst.btnkey if inst else "")
            else:
                tools.set_process_box(f'{s["line"]}\n{startTime} --> {endTime}\n{text}\n\n', func_name="set_subtitle")
        return raw_subtitles
    except Exception as e:
        raise Exception(f'whole all {str(e)}')
    finally:
        try:
            if model:
                del model
        except Exception:
            pass


#
def match_target_amplitude(sound, target_dBFS):
    change_in_dBFS = target_dBFS - sound.dBFS
    return sound.apply_gain(change_in_dBFS)


# split audio by silence
def shorten_voice(normalized_sound, max_interval=60000):
    normalized_sound = match_target_amplitude(normalized_sound, -20.0)
    nonsilent_data = []
    audio_chunks = detect_nonsilent(normalized_sound, min_silence_len=int(config.settings['voice_silence']),
                                    silence_thresh=-20 - 25)
    for i, chunk in enumerate(audio_chunks):
        start_time, end_time = chunk
        n = 0
        while end_time - start_time >= max_interval:
            n += 1
            # new_end = start_time + max_interval+buffer
            new_end = start_time + max_interval
            new_start = start_time
            nonsilent_data.append((new_start, new_end, True))
            start_time += max_interval
        nonsilent_data.append((start_time, end_time, False))
    return nonsilent_data


# 预先分割识别
def split_recogn(*, detect_language=None, audio_file=None, cache_folder=None, model_name="base", set_p=True, inst=None,
                 is_cuda=None):
    if set_p:
        tools.set_process(config.transobj['fengeyinpinshuju'],btnkey=inst.btnkey if inst else "")
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    noextname = os.path.basename(audio_file)
    tmp_path = f'{cache_folder}/{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path, 0o777, exist_ok=True)
        except:
            raise Exception(config.transobj["createdirerror"])
    if not tools.vail_file(audio_file):
        raise Exception(f'[error]not exists {audio_file}')
    normalized_sound = AudioSegment.from_wav(audio_file)  # -20.0
    nonslient_file = f'{tmp_path}/detected_voice.json'
    if tools.vail_file(nonslient_file):
        with open(nonslient_file, 'r') as infile:
            nonsilent_data = json.load(infile)
    else:
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        if inst and inst.precent < 55:
            inst.precent += 0.1
        tools.set_process(config.transobj['qiegeshujuhaoshi'],btnkey=inst.btnkey if inst else "")
        nonsilent_data = shorten_voice(normalized_sound)
        with open(nonslient_file, 'w') as outfile:
            json.dump(nonsilent_data, outfile)

    raw_subtitles = []
    total_length = len(nonsilent_data)
    model = None
    try:
        model = WhisperModel(model_name, device="cuda" if is_cuda else "cpu",
                             compute_type="float32" if model_name.startswith('distil-') else config.settings[
                                 'cuda_com_type'],
                             download_root=config.rootdir + "/models",
                             local_files_only=True)
    except Exception as e:
        raise Exception(str(e.args))
    for i, duration in enumerate(nonsilent_data):
        if config.exit_soft :
            del model
            return False
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            return None
        start_time, end_time, buffered = duration

        chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
        audio_chunk = normalized_sound[start_time:end_time]
        audio_chunk.export(chunk_filename, format="wav")

        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            raise Exception("stop")
        text = ""
        try:
            segments, _ = model.transcribe(chunk_filename,
                                           beam_size=config.settings['beam_size'],
                                           best_of=config.settings['best_of'],
                                           condition_on_previous_text=config.settings['condition_on_previous_text'],
                                           temperature=0 if config.settings['temperature'] == 0 else [0.0, 0.2, 0.4,
                                                                                                      0.6, 0.8, 1.0],
                                           vad_filter=bool(config.settings['vad']),
                                           vad_parameters=dict(
                                               min_silence_duration_ms=config.settings['overall_silence'],
                                               max_speech_duration_s=config.settings['overall_maxsecs']
                                           ),
                                           word_timestamps=True,
                                           language=detect_language,
                                           initial_prompt=None if detect_language != 'zh' else config.settings[
                                               'initial_prompt_zh'], )
            for t in segments:

                if detect_language == 'zh' and t.text == config.settings['initial_prompt_zh']:
                    continue
                start_time, end_time, buffered = duration
                text = t.text
                text = f"{text.capitalize()}. ".replace('&#39;', "'")
                text = re.sub(r'&#\d+;', '', text).strip().strip('.')
                if detect_language == 'zh' and text == config.settings['initial_prompt_zh']:
                    continue
                if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text):
                    continue
                end_time = start_time + t.words[-1].end * 1000
                start_time += t.words[0].start * 1000
                start = timedelta(milliseconds=start_time)
                stmp = str(start).split('.')
                if len(stmp) == 2:
                    start = f'{stmp[0]},{int(int(stmp[-1]) / 1000)}'
                end = timedelta(milliseconds=end_time)
                etmp = str(end).split('.')
                if len(etmp) == 2:
                    end = f'{etmp[0]},{int(int(etmp[-1]) / 1000)}'
                srt_line = {"line": len(raw_subtitles) + 1, "time": f"{start} --> {end}", "text": text}
                raw_subtitles.append(srt_line)
                if set_p:
                    if inst and inst.precent < 55:
                        inst.precent += 0.1
                    tools.set_process(f"{config.transobj['yuyinshibiejindu']} {srt_line['line']}",btnkey=inst.btnkey if inst else "")
                    msg = f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n"
                    tools.set_process(msg, 'subtitle')
                else:
                    tools.set_process_box(f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n",
                                          func_name="set_subtitle")
        except Exception as e:
            del model
            raise Exception(str(e.args))

    if set_p:
        tools.set_process(f"{config.transobj['yuyinshibiewancheng']} / {len(raw_subtitles)}", 'logs',btnkey=inst.btnkey if inst else "")
    # 写入原语言字幕到目标文件夹
    return raw_subtitles


# split audio by silence
def shorten_voice_old(normalized_sound):
    normalized_sound = match_target_amplitude(normalized_sound, -20.0)
    max_interval = config.settings['interval_split'] * 1000
    buffer = int(config.settings['voice_silence'])
    nonsilent_data = []
    audio_chunks = detect_nonsilent(normalized_sound, min_silence_len=int(config.settings['voice_silence']),
                                    silence_thresh=-20 - 25)
    # print(audio_chunks)
    for i, chunk in enumerate(audio_chunks):

        start_time, end_time = chunk
        n = 0
        while end_time - start_time >= max_interval:
            n += 1
            # new_end = start_time + max_interval+buffer
            new_end = start_time + max_interval + buffer
            new_start = start_time
            nonsilent_data.append((new_start, new_end, True))
            start_time += max_interval
        nonsilent_data.append((start_time, end_time, False))
    return nonsilent_data


# openai
def split_recogn_openai(*, detect_language=None, audio_file=None, cache_folder=None, model_name="base", set_p=True,
                        inst=None, is_cuda=None):
    import whisper
    if set_p:
        tools.set_process(config.transobj['fengeyinpinshuju'],btnkey=inst.btnkey if inst else "")
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    noextname = os.path.basename(audio_file)
    tmp_path = f'{cache_folder}/{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path, 0o777, exist_ok=True)
        except:
            raise Exception(config.transobj["createdirerror"])
    if not tools.vail_file(audio_file):
        raise Exception(f'[error]not exists {audio_file}')
    normalized_sound = AudioSegment.from_wav(audio_file)  # -20.0
    nonslient_file = f'{tmp_path}/detected_voice.json'
    if tools.vail_file(nonslient_file):
        with open(nonslient_file, 'r') as infile:
            nonsilent_data = json.load(infile)
    else:
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        if inst and inst.precent < 55:
            inst.precent += 0.1
        tools.set_process(config.transobj['qiegeshujuhaoshi'],btnkey=inst.btnkey if inst else "")
        nonsilent_data = shorten_voice_old(normalized_sound)
        with open(nonslient_file, 'w') as outfile:
            json.dump(nonsilent_data, outfile)

    raw_subtitles = []
    total_length = len(nonsilent_data)
    model = None
    try:
        model = whisper.load_model(model_name,
                                   device="cuda" if is_cuda else "cpu",
                                   download_root=config.rootdir + "/models"
                                   )
    except Exception as e:
        raise Exception(str(e.args))
    for i, duration in enumerate(nonsilent_data):
        if config.exit_soft :
            del model
            return False
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            raise Exception("stop")
        start_time, end_time, buffered = duration
        if start_time == end_time:
            end_time += int(config.settings['voice_silence'])
        chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
        audio_chunk = normalized_sound[start_time:end_time]
        audio_chunk.export(chunk_filename, format="wav")

        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            raise Exception("stop")
        text = ""
        try:
            tr = model.transcribe(chunk_filename,
                                  language=detect_language,
                                  initial_prompt=None if detect_language != 'zh' else config.settings[
                                      'initial_prompt_zh'],
                                  condition_on_previous_text=config.settings['condition_on_previous_text']
                                  )
            for t in tr['segments']:
                if detect_language == 'zh' and t['text'].strip() == config.settings['initial_prompt_zh']:
                    continue
                text += t['text'] + " "
        except Exception as e:
            del model
            raise Exception(str(e.args))

        text = f"{text.capitalize()}. ".replace('&#39;', "'")
        text = re.sub(r'&#\d+;', '', text).strip()
        if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text):
            continue
        start = timedelta(milliseconds=start_time)
        stmp = str(start).split('.')
        if len(stmp) == 2:
            start = f'{stmp[0]},{int(int(stmp[-1]) / 1000)}'
        end = timedelta(milliseconds=end_time)
        etmp = str(end).split('.')
        if len(etmp) == 2:
            end = f'{etmp[0]},{int(int(etmp[-1]) / 1000)}'
        srt_line = {"line": len(raw_subtitles) + 1, "time": f"{start} --> {end}", "text": text}
        raw_subtitles.append(srt_line)
        if set_p:
            if inst and inst.precent < 55:
                inst.precent += round(srt_line['line'] * 5 / total_length, 2)
            tools.set_process(f"{config.transobj['yuyinshibiejindu']} {srt_line['line']}/{total_length}",btnkey=inst.btnkey if inst else "")
            msg = f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n"
            tools.set_process(msg, 'subtitle')
        else:
            tools.set_process_box(f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n",
                                  func_name="set_subtitle")
    if set_p:
        tools.set_process(f"{config.transobj['yuyinshibiewancheng']} / {len(raw_subtitles)}", 'logs',btnkey=inst.btnkey if inst else "")
    # 写入原语言字幕到目标文件夹
    return raw_subtitles


# 均等分割识别
def split_recogn_old(*, detect_language=None, audio_file=None, cache_folder=None, model_name="base", set_p=True,
                     inst=None, is_cuda=None):
    if set_p:
        tools.set_process(config.transobj['fengeyinpinshuju'],btnkey=inst.btnkey if inst else "")
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    noextname = os.path.basename(audio_file)
    tmp_path = f'{cache_folder}/{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path, 0o777, exist_ok=True)
        except:
            raise Exception(config.transobj["createdirerror"])
    if not tools.vail_file(audio_file):
        raise Exception(f'[error]not exists {audio_file}')
    normalized_sound = AudioSegment.from_wav(audio_file)  # -20.0
    nonslient_file = f'{tmp_path}/detected_voice.json'
    if tools.vail_file(nonslient_file):
        with open(nonslient_file, 'r') as infile:
            nonsilent_data = json.load(infile)
    else:
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        nonsilent_data = shorten_voice_old(normalized_sound)
        with open(nonslient_file, 'w') as outfile:
            json.dump(nonsilent_data, outfile)

    raw_subtitles = []
    total_length = len(nonsilent_data)
    start_t = time.time()
    try:
        model = WhisperModel(model_name, device="cuda" if config.params['cuda'] else "cpu",
                             compute_type="float32" if model_name.startswith('distil-') else config.settings[
                                 'cuda_com_type'],
                             download_root=config.rootdir + "/models",
                             local_files_only=True)
    except Exception as e:
        raise Exception(str(e.args))
    for i, duration in enumerate(nonsilent_data):
        if config.exit_soft:
            return False
        # config.temp = {}
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            raise Exception("stop")
        start_time, end_time, buffered = duration
        if start_time == end_time:
            end_time += int(config.settings['voice_silence'])

        chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
        audio_chunk = normalized_sound[start_time:end_time]
        audio_chunk.export(chunk_filename, format="wav")

        if config.current_status != 'ing' and config.box_recogn != 'ing':
            del model
            raise Exception("stop")
        text = ""
        try:
            segments, _ = model.transcribe(chunk_filename,
                                           beam_size=5,
                                           best_of=5,
                                           condition_on_previous_text=True,
                                           language=detect_language,
                                           initial_prompt=None if detect_language != 'zh' else config.settings[
                                               'initial_prompt_zh'], )
            for t in segments:
                text += t.text + " "
        except Exception as e:
            del model
            raise Exception(str(e.args))

        text = f"{text.capitalize()}. ".replace('&#39;', "'")
        text = re.sub(r'&#\d+;', '', text).strip()
        if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text):
            continue
        start = timedelta(milliseconds=start_time)
        stmp = str(start).split('.')
        if len(stmp) == 2:
            start = f'{stmp[0]},{int(int(stmp[-1]) / 1000)}'
        end = timedelta(milliseconds=end_time)
        etmp = str(end).split('.')
        if len(etmp) == 2:
            end = f'{etmp[0]},{int(int(etmp[-1]) / 1000)}'
        srt_line = {"line": len(raw_subtitles) + 1, "time": f"{start} --> {end}", "text": text}
        raw_subtitles.append(srt_line)
        if set_p:
            if inst and inst.precent < 55:
                inst.precent += round(srt_line['line'] * 5 / total_length, 2)
            tools.set_process(f"{config.transobj['yuyinshibiejindu']} {srt_line['line']}/{total_length}",btnkey=inst.btnkey if inst else "")
            msg = f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n"
            tools.set_process(msg, 'subtitle')
        else:
            tools.set_process_box(f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n",
                                  func_name="set_subtitle")
    if set_p:
        tools.set_process(f"{config.transobj['yuyinshibiewancheng']} / {len(raw_subtitles)}", 'logs',btnkey=inst.btnkey if inst else "")
    # 写入原语言字幕到目标文件夹
    return raw_subtitles


def google_recogn(*, detect_language=None, audio_file=None, cache_folder=None, set_p=True, inst=None):
    if set_p:
        tools.set_process(config.transobj['fengeyinpinshuju'],btnkey=inst.btnkey if inst else "")
    if config.current_status != 'ing' and config.box_recogn != 'ing':
        return False
    proxy = tools.set_proxy()
    if proxy:
        os.environ['http_proxy'] = proxy
        os.environ['https_proxy'] = proxy
    noextname = os.path.basename(audio_file)
    tmp_path = f'{cache_folder}/{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path, 0o777, exist_ok=True)
        except:
            raise Exception(config.transobj["createdirerror"])
    if not tools.vail_file(audio_file):
        raise Exception(f'[error]not exists {audio_file}')
    normalized_sound = AudioSegment.from_wav(audio_file)  # -20.0
    nonslient_file = f'{tmp_path}/detected_voice.json'
    if tools.vail_file(nonslient_file):
        with open(nonslient_file, 'r') as infile:
            nonsilent_data = json.load(infile)
    else:
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        nonsilent_data = shorten_voice_old(normalized_sound)
        with open(nonslient_file, 'w') as outfile:
            json.dump(nonsilent_data, outfile)

    raw_subtitles = []
    total_length = len(nonsilent_data)
    start_t = time.time()

    import speech_recognition as sr
    try:
        recognizer = sr.Recognizer()
    except Exception as e:
        raise Exception(f'使用Google识别需要设置代理')

    for i, duration in enumerate(nonsilent_data):
        if config.exit_soft :
            return False
        # config.temp = {}
        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        start_time, end_time, buffered = duration
        if start_time == end_time:
            end_time += int(config.settings['voice_silence'])

        chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
        audio_chunk = normalized_sound[start_time:end_time]
        audio_chunk.export(chunk_filename, format="wav")

        if config.current_status != 'ing' and config.box_recogn != 'ing':
            raise Exception("stop")
        text = ""
        try:
            with sr.AudioFile(chunk_filename) as source:
                # Record the audio data
                audio_data = recognizer.record(source)
                try:
                    # Recognize the speech
                    text = recognizer.recognize_google(audio_data, language=detect_language)
                except sr.UnknownValueError:
                    text = ""
                    print("Speech recognition could not understand the audio.")
                except sr.RequestError as e:
                    raise Exception(f"Google识别出错，请检查代理是否正确：{e}")
        except Exception as e:
            raise Exception('Google识别出错：' + str(e.args))

        text = f"{text.capitalize()}. ".replace('&#39;', "'")
        text = re.sub(r'&#\d+;', '', text).strip()
        if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text):
            continue
        start = timedelta(milliseconds=start_time)
        stmp = str(start).split('.')
        if len(stmp) == 2:
            start = f'{stmp[0]},{int(int(stmp[-1]) / 1000)}'
        end = timedelta(milliseconds=end_time)
        etmp = str(end).split('.')
        if len(etmp) == 2:
            end = f'{etmp[0]},{int(int(etmp[-1]) / 1000)}'
        srt_line = {"line": len(raw_subtitles) + 1, "time": f"{start} --> {end}", "text": text}
        raw_subtitles.append(srt_line)
        if set_p:
            if inst and inst.precent < 55:
                inst.precent += round(srt_line['line'] * 5 / total_length, 2)
            tools.set_process(f"{config.transobj['yuyinshibiejindu']} {srt_line['line']}/{total_length}",btnkey=inst.btnkey if inst else "")
            msg = f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n"
            tools.set_process(msg, 'subtitle')
        else:
            tools.set_process_box(f"{srt_line['line']}\n{srt_line['time']}\n{srt_line['text']}\n\n",
                                  func_name="set_subtitle")
    if set_p:
        tools.set_process(f"{config.transobj['yuyinshibiewancheng']} / {len(raw_subtitles)}", 'logs',btnkey=inst.btnkey if inst else "")
    # 写入原语言字幕到目标文件夹
    return raw_subtitles
