import importlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core import Project, Character, Scene, compile_prompt, shot_lengths, validate_locks
from provider import MiniMax


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv('FILM_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
    import app
    importlib.reload(app)
    # Explicit worker stepping keeps lifecycle tests deterministic.
    app.init_db()
    return app, TestClient(app.app)


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (512, 768), '#777766').save(output, format='PNG')
    return output.getvalue()


def test_all_scene_lengths_exact_and_supported():
    for duration in range(4, 61):
        shots = shot_lengths(duration)
        assert sum(shots) == duration
        assert all(4 <= s <= 15 for s in shots)
    assert shot_lengths(60) == [15, 15, 15, 15]
    assert shot_lengths(16) == [8, 8]


def test_protected_fields_require_separate_unlock():
    old = Project(characters=[Character(name='Priest', wardrobe='black cassock')])
    changed = old.model_copy(deep=True)
    changed.style_locked = False
    changed.style = 'New style'
    with pytest.raises(ValueError):
        validate_locks(old, changed)
    changed = old.model_copy(deep=True)
    changed.characters[0].locked = False
    changed.characters[0].wardrobe = 'White suit'
    with pytest.raises(ValueError):
        validate_locks(old, changed)


def test_prompt_anchors_and_context_preservation():
    char = Character(name='Exorcist', wardrobe='weathered black cassock', appearance='short dark hair', references=['sheet.jpg'])
    scene = Scene(cast=[char.id], controls={'Horror': {'emotion': 85}, 'Drama': {'emotion': 20}}, direction='He enters the church.')
    project = Project(genre='Horror', characters=[char], scenes=[scene])
    prompt = compile_prompt(project, scene)
    assert 'weathered black cassock' in prompt and 'reference image(s) 1' in prompt
    assert 'Dread: 85/100' in prompt and project.style in prompt
    project.genre = 'Drama'
    assert 'Emotional intensity: 20/100' in compile_prompt(project, scene)
    assert scene.controls['Horror']['emotion'] == 85


def test_persistence_upload_revision_and_locking(studio):
    app, client = studio
    project = client.post('/api/projects', json={'title': 'The Last Three Priests'}).json()
    uploaded = client.post('/api/references', content=image_bytes()).json()['file']
    char = Character(name='Exorcist', appearance='Dark hair', wardrobe='Black cassock', references=[uploaded]).model_dump()
    project['characters'].append(char)
    response = client.put('/api/projects/' + project['id'], json=project)
    assert response.status_code == 200
    saved = response.json()
    assert client.put('/api/projects/' + project['id'], json=project).status_code == 409
    saved['characters'][0]['wardrobe'] = 'White suit'
    assert client.put('/api/projects/' + project['id'], json=saved).status_code == 400
    assert client.get('/api/projects/' + project['id']).json()['characters'][0]['wardrobe'] == 'Black cassock'
    assert client.post('/api/references', content=b'not an image').status_code == 400
    assert client.get('/media/' + uploaded).status_code == 200
    assert client.post('/api/projects', json={'title': 'Attack'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/status', headers={'Host':'evil.example'}).status_code == 403


def ready_project(app, client):
    p = client.post('/api/projects', json={'title':'Film'}).json()
    ref = client.post('/api/references', content=image_bytes()).json()['file']
    char = Character(name='A', appearance='Dark hair', wardrobe='Black coat', references=[ref])
    scene = Scene(title='Rain', direction='A walks through the rain', duration=16, cast=[char.id])
    p['characters'] = [char.model_dump()]
    p['scenes'] = [scene.model_dump()]
    p = client.put('/api/projects/' + p['id'], json=p).json()
    return p, scene


def test_generation_gate_snapshots_and_duplicate_guard(studio, monkeypatch):
    app, client = studio
    p, scene = ready_project(app, client)
    url = f"/api/projects/{p['id']}/generate"
    assert client.post(url, json={'scene_id':scene.id}).status_code == 400
    monkeypatch.setenv('MINIMAX_API_KEY','test')
    with patch('shutil.which', return_value='/usr/bin/ffmpeg'):
        job = client.post(url, json={'scene_id':scene.id}).json()
        assert job['state'] == 'queued'
        assert job['snapshot']['characters'][0]['wardrobe'] == 'Black coat'
        assert client.post(url, json={'scene_id':scene.id}).status_code == 409
    assert [s['duration'] for s in job['shots']] == [8,8]


def test_worker_resume_and_approval_export_gate(studio, monkeypatch):
    app, client = studio
    p, scene = ready_project(app, client)
    monkeypatch.setenv('MINIMAX_API_KEY', 'test')
    job = client.post(f"/api/projects/{p['id']}/generate", json={'scene_id':scene.id}).json()
    class Provider:
        calls = 0
        def shot_directions(self, direction, lengths): return ['Approaches door', 'Opens door']
        def submit(self, prompt, references, duration):
            assert references and 'Black coat' in prompt
            Provider.calls += 1
            return str(Provider.calls)
        def poll(self, task): return {'status':'succeeded', 'content':{'url':'https://cdn.hailuoai.com/a.mp4'}}
    monkeypatch.setattr(app, 'MiniMax', Provider)
    monkeypatch.setattr(app.media, 'download', lambda url,path: path.write_bytes(b'clip'))
    monkeypatch.setattr(app.media, 'assemble', lambda items,path: path.write_bytes(b'film'))
    for _ in range(6):
        app.advance(app.get_job(job['id']))
    result = app.get_job(job['id'])
    assert result['state'] == 'complete' and Provider.calls == 2
    payload={'items':[{'take_id':job['id'], 'start':0,'end':16}]}
    assert client.post(f"/api/projects/{p['id']}/export", json=payload).status_code == 400
    assert client.post(f"/api/jobs/{job['id']}/review", json={'review':'approved'}).status_code == 200
    exported = client.post(f"/api/projects/{p['id']}/export", json=payload).json()
    app.advance(exported)
    assert app.get_job(exported['id'])['state'] == 'complete'


def test_interrupted_submission_never_resubmits(studio):
    app, client = studio
    job={'id':'a'*32,'kind':'generation','project_id':'b'*32,'scene_id':'c'*32,'state':'submitting'}
    app.put_job(job)
    app.init_db()
    assert app.get_job(job['id'])['state'] == 'needs_attention'


def test_provider_payload_and_response(monkeypatch, tmp_path):
    monkeypatch.setenv('MINIMAX_API_KEY','secret')
    provider=MiniMax()
    requests=[]
    def request(method,path,body=None):
        requests.append((method,path,body))
        return {'task_id':'123'} if method=='POST' else {'task':{'status':'succeeded'}}
    monkeypatch.setattr(provider, 'request', request)
    ref=tmp_path/'ref.jpg';ref.write_bytes(image_bytes())
    assert provider.submit('Preserve identity', [ref], 8) == '123'
    payload=requests[0][2]
    assert payload['model']=='MiniMax-H3' and payload['resolution']=='2K'
    assert payload['content'][1]['role']=='reference_image'
    assert payload['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert provider.poll('123')['status']=='succeeded'
    assert requests[-1][1]=='/v2/query/video_generation/123'


def test_real_ffmpeg_trim_stitch_mute(tmp_path):
    import media
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg not installed')
    source=tmp_path/'source.mp4'
    media.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=24',
               '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1',
               '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)])
    output=tmp_path/'film.mp4'
    media.assemble([{'path':source,'start':.1,'end':.7,'mute':True}, {'path':source,'start':0,'end':.5}], output)
    seconds,audio=media.duration(output)
    assert 1.05 <= seconds <= 1.25 and audio
    info=json.loads(media.run(['ffprobe','-v','error','-show_streams','-of','json',str(output)]))
    video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert video['width']==1920 and video['height']==1080
