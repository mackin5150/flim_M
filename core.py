"""Validated film documents, fixed direction presets, and continuity-aware prompts."""
import math
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field, model_validator


def uid():
    return uuid4().hex


PRESETS = {
    'Drama': {'emotion': 'Emotional intensity', 'lighting': 'Light contrast', 'camera': 'Camera elevation', 'intimacy': 'Intimacy'},
    'Horror': {'emotion': 'Dread', 'lighting': 'Shadow depth', 'camera': 'Camera elevation', 'unease': 'Unease'},
    'Science fiction': {'emotion': 'Wonder', 'lighting': 'Light contrast', 'camera': 'Camera elevation', 'scale': 'World scale'},
    'Action': {'emotion': 'Urgency', 'lighting': 'Light contrast', 'camera': 'Camera elevation', 'energy': 'Kinetic energy'},
    'Fantasy': {'emotion': 'Awe', 'lighting': 'Light contrast', 'camera': 'Camera elevation', 'magic': 'Supernatural intensity'},
}
SCENE_PRESETS = {
    'Dialogue': {'pace': 'Conversational pace'},
    'Establishing': {'breadth': 'Environmental breadth'},
    'Chase': {'speed': 'Movement speed'},
    'Revelation': {'reveal': 'Reveal intensity'},
}


class Character(BaseModel):
    id: str = Field(default_factory=uid, pattern=r'^[a-f0-9]{32}$')
    name: str = Field(min_length=1, max_length=80)
    appearance: str = Field(default='', max_length=2000)
    wardrobe: str = Field(default='', max_length=2000)
    references: list[str] = Field(default_factory=list, max_length=3)
    locked: bool = True


class Scene(BaseModel):
    id: str = Field(default_factory=uid, pattern=r'^[a-f0-9]{32}$')
    title: str = Field(default='Untitled scene', min_length=1, max_length=120)
    direction: str = Field(default='', max_length=8000)
    duration: int = Field(default=12, ge=4, le=60)
    kind: Literal['Dialogue', 'Establishing', 'Chase', 'Revelation'] = 'Establishing'
    cast: list[str] = Field(default_factory=list, max_length=9)
    # Values survive switching genre/type; the schema of each preset is fixed.
    controls: dict[str, dict[str, int]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def controls_valid(self):
        if len(self.controls) > 20:
            raise ValueError('Too many control presets')
        for key, values in self.controls.items():
            if key not in PRESETS and key not in SCENE_PRESETS:
                raise ValueError('Unknown preset')
            allowed = PRESETS.get(key, SCENE_PRESETS.get(key))
            if set(values) - set(allowed) or any(not 0 <= v <= 100 for v in values.values()):
                raise ValueError('Invalid direction controls')
        if len(set(self.cast)) != len(self.cast):
            raise ValueError('Duplicate cast')
        return self


class Project(BaseModel):
    id: str = Field(default_factory=uid, pattern=r'^[a-f0-9]{32}$')
    revision: int = 0
    title: str = Field(default='Untitled film', min_length=1, max_length=120)
    genre: Literal['Drama', 'Horror', 'Science fiction', 'Action', 'Fantasy'] = 'Drama'
    style: str = Field(default='Grounded cinematic realism. Natural skin texture, restrained color, soft film grain, coherent production design.', max_length=5000)
    style_locked: bool = True
    script: str = Field(default='', max_length=60000)
    characters: list[Character] = Field(default_factory=list, max_length=30)
    scenes: list[Scene] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def cast_valid(self):
        ids = [c.id for c in self.characters]
        if len(set(ids)) != len(ids) or len({s.id for s in self.scenes}) != len(self.scenes):
            raise ValueError('Duplicate IDs')
        for scene in self.scenes:
            if set(scene.cast) - set(ids):
                raise ValueError('Scene references unknown character')
        return self


def shot_lengths(seconds):
    if not 4 <= seconds <= 60:
        raise ValueError('Scenes must be 4–60 seconds')
    count = math.ceil(seconds / 15)
    base, extra = divmod(seconds, count)
    return [base + (i < extra) for i in range(count)]


def compile_prompt(project: Project, scene: Scene):
    lines = [f'VISUAL BIBLE (maintain throughout): {project.style}', f'Genre: {project.genre}. Scene type: {scene.kind}.']
    ref_index = 1
    for character in project.characters:
        if character.id not in scene.cast:
            continue
        indexes = ', '.join(str(i) for i in range(ref_index, ref_index + len(character.references)))
        lines.append(f'CHARACTER {character.name}: match reference image(s) {indexes}. Appearance: {character.appearance}. WARDROBE: {character.wardrobe}. Preserve face, hair, age, body proportions, costume colors, fabrics and accessories. Do not exchange identities or costumes.')
        ref_index += len(character.references)
    lines.append(f'DIRECTOR: {scene.direction}')
    for context, schema in [(project.genre, PRESETS[project.genre]), (scene.kind, SCENE_PRESETS[scene.kind])]:
        for key, label in schema.items():
            value = scene.controls.get(context, {}).get(key, 50)
            if key == 'camera':
                instruction = 'low angle' if value < 34 else 'eye level' if value < 67 else 'high angle'
                lines.append(f'Camera elevation: {instruction} ({value}/100).')
            else:
                lines.append(f'{label}: {value}/100 (0=subtle/soft/slow, 100=strong/hard/fast).')
    lines.append('Respect the visual bible and character references over any contradictory styling instruction. Continuous coherent action. No captions, logos, costume changes or unexplained new characters.')
    return '\n'.join(lines)


def validate_locks(old: Project, new: Project):
    # Unlock in a separate saved revision before changing protected fields.
    if old.style_locked and (old.style != new.style):
        raise ValueError('Unlock and save the visual bible before editing it')
    for old_char in old.characters:
        current = next((c for c in new.characters if c.id == old_char.id), None)
        if old_char.locked and (current is None or any(getattr(old_char, k) != getattr(current, k) for k in ('name', 'appearance', 'wardrobe', 'references'))):
            raise ValueError('Unlock and save the character before editing or removing it')
