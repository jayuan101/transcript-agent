// LLM providers and STT engines, mirrored from the backend (app.py _PROVIDERS
// and transcript_agent.STT_ENGINES). `type` and `baseUrl` are what the /api
// endpoint forwards to transcript_agent.run() as `provider` / `base_url`.

export const PROVIDERS = {
  'Claude (Anthropic)': {
    type: 'anthropic',
    baseUrl: null,
    keyPlaceholder: 'sk-ant-api03-…',
    models: [
      'claude-opus-4-8',
      'claude-sonnet-4-6',
      'claude-haiku-4-5-20251001',
      'claude-3-5-sonnet-20241022',
      'claude-3-5-haiku-20241022',
    ],
  },
  OpenAI: {
    type: 'openai',
    baseUrl: null,
    keyPlaceholder: 'sk-…',
    models: ['gpt-4.1', 'gpt-4.1-mini', 'gpt-4.1-nano', 'gpt-4o', 'gpt-4o-mini', 'o3', 'o3-mini', 'o4-mini'],
  },
  'Google Gemini': {
    type: 'openai_compat',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/',
    keyPlaceholder: 'AIzaSy…',
    models: ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.0-flash-lite'],
  },
  Groq: {
    type: 'openai_compat',
    baseUrl: 'https://api.groq.com/openai/v1',
    keyPlaceholder: 'gsk_…',
    models: ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'deepseek-r1-distill-llama-70b', 'qwen-qwq-32b', 'gemma2-9b-it'],
  },
  Mistral: {
    type: 'openai_compat',
    baseUrl: 'https://api.mistral.ai/v1',
    keyPlaceholder: '…',
    models: ['mistral-large-latest', 'mistral-small-latest', 'mistral-nemo-latest', 'codestral-latest'],
  },
  'Together AI': {
    type: 'openai_compat',
    baseUrl: 'https://api.together.xyz/v1',
    keyPlaceholder: '…',
    models: [
      'meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo',
      'meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo',
      'Qwen/Qwen2.5-72B-Instruct-Turbo',
      'Qwen/QwQ-32B',
      'google/gemma-3-27b-it',
    ],
  },
  Perplexity: {
    type: 'openai_compat',
    baseUrl: 'https://api.perplexity.ai',
    keyPlaceholder: 'pplx-…',
    models: ['sonar-pro', 'sonar', 'sonar-reasoning-pro', 'sonar-reasoning', 'r1-1776'],
  },
  'Ollama (Local)': {
    type: 'openai_compat',
    baseUrl: 'http://localhost:11434/v1',
    keyPlaceholder: 'none required',
    models: [
      'gemma3:27b', 'gemma3:12b',
      'llama4:maverick', 'llama4:scout', 'llama3.3', 'qwen3:235b-a22b', 'qwen2.5:72b', 'deepseek-r1:70b',
      'qwen3:32b', 'qwen3:30b-a3b', 'qwen2.5:32b', 'deepseek-r1:32b',
      'qwen3:14b', 'phi4', 'phi4-mini', 'qwen3:8b', 'qwen2.5:14b', 'llama3.2',
      'devstral', 'mistral-small3.1', 'mistral',
    ],
  },
  'xAI (Grok)': {
    type: 'openai_compat',
    baseUrl: 'https://api.x.ai/v1',
    keyPlaceholder: 'xai-…',
    models: ['grok-4.3', 'grok-4-heavy', 'grok-4-fast', 'grok-4.20-reasoning'],
  },
  DeepSeek: {
    type: 'openai_compat',
    baseUrl: 'https://api.deepseek.com/v1',
    keyPlaceholder: 'sk-…',
    models: ['deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-chat', 'deepseek-reasoner'],
  },
  OpenRouter: {
    type: 'openai_compat',
    baseUrl: 'https://openrouter.ai/api/v1',
    keyPlaceholder: 'sk-or-…',
    models: [
      'anthropic/claude-opus-4-8', 'anthropic/claude-sonnet-4-6', 'openai/gpt-5.5', 'openai/gpt-4.1',
      'google/gemini-3.5-flash', 'google/gemini-2.5-pro-preview', 'meta-llama/llama-4-maverick',
      'deepseek/deepseek-r1', 'x-ai/grok-4.3', 'mistralai/mistral-large-2411',
    ],
  },
  Cerebras: {
    type: 'openai_compat',
    baseUrl: 'https://api.cerebras.ai/v1',
    keyPlaceholder: 'csk-…',
    models: ['gpt-oss-120b', 'zai-glm-4.7', 'llama4-maverick', 'llama4-scout', 'llama-3.3-70b', 'qwen-3-32b', 'deepseek-r1-distill-70b'],
  },
  Cohere: {
    type: 'openai_compat',
    baseUrl: 'https://api.cohere.com/compatibility/v1',
    keyPlaceholder: '…',
    models: ['command-a-plus-05-2026', 'command-a-03-2025', 'command-r-plus-08-2024', 'command-r-08-2024', 'command-r7b-12-2024'],
  },
}

export const STT_ENGINES = {
  whisper_local: 'Whisper (Local / Offline)',
  openai_whisper: 'OpenAI Whisper API',
  groq_whisper: 'Groq Whisper',
  deepgram: 'Deepgram',
  assemblyai: 'AssemblyAI',
  google_stt: 'Google Cloud STT',
  azure_speech: 'Azure Speech',
  elevenlabs: 'ElevenLabs Scribe',
  revai: 'Rev.ai',
}

export const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3', 'turbo']

// One combined "transcription" picker: engine + that engine's own model in a
// single grouped dropdown. `cloud` engines need an API key; `local` Whisper is
// free/offline and uses the tiny→large size models. Each option's value encodes
// "engine|model" so the form can split it back into stt_engine + whisper_model
// (local) or stt_model (cloud) for the backend.
// Mirrors app.py `_STT_MODELS` — every engine with its full model lineup so the
// user can pick any engine + model. Whisper-local uses the size models; cloud
// engines list their own model ids. Models are [value, displayLabel] pairs.
const _m = (...ids) => ids.map((id) => [id, id])

export const STT_CATALOG = [
  {
    engine: 'whisper_local', cloud: false,
    label: 'Whisper (Local / Offline)',
    models: [
      ['tiny', 'tiny — fastest'],
      ['base', 'base — fast (default)'],
      ['small', 'small — better'],
      ['medium', 'medium — more accurate'],
      ['large-v2', 'large-v2 — best accuracy'],
      ['large-v3', 'large-v3 — best accuracy (newer)'],
      ['turbo', 'turbo — large quality, ~8x faster'],
    ],
    default: 'base',
  },
  { engine: 'openai_whisper', cloud: true, label: 'OpenAI Whisper API',
    models: _m('whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe'), default: 'whisper-1' },
  { engine: 'groq_whisper', cloud: true, label: 'Groq Whisper',
    models: _m('whisper-large-v3-turbo', 'whisper-large-v3', 'distil-whisper-large-v3-en'), default: 'whisper-large-v3-turbo' },
  { engine: 'deepgram', cloud: true, label: 'Deepgram', diarizes: true,
    models: _m('nova-3', 'nova-2', 'nova', 'enhanced', 'base'), default: 'nova-3' },
  { engine: 'assemblyai', cloud: true, label: 'AssemblyAI', diarizes: true,
    models: _m('best', 'nano', 'slam-1'), default: 'best' },
  { engine: 'google_stt', cloud: true, label: 'Google Cloud STT',
    models: _m('latest_long', 'latest_short', 'command_and_search', 'phone_call'), default: 'latest_long' },
  { engine: 'azure_speech', cloud: true, label: 'Azure Speech',
    models: _m('conversation', 'dictation', 'command_and_search'), default: 'conversation' },
  { engine: 'elevenlabs', cloud: true, label: 'ElevenLabs Scribe',
    models: _m('scribe_v1'), default: 'scribe_v1' },
  { engine: 'revai', cloud: true, label: 'Rev.ai',
    models: _m('machine', 'fusion'), default: 'machine' },
]

// Look up an engine entry by key.
export const sttEngineByKey = (key) => STT_CATALOG.find((e) => e.engine === key)
export const REPORT_STYLES = ['formal', 'casual', 'executive', 'bullet']
export const LANGUAGES = [
  { code: '', label: 'Auto-detect' },
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Spanish' },
  { code: 'fr', label: 'French' },
  { code: 'de', label: 'German' },
  { code: 'it', label: 'Italian' },
  { code: 'pt', label: 'Portuguese' },
  { code: 'zh', label: 'Chinese' },
  { code: 'ja', label: 'Japanese' },
  { code: 'ko', label: 'Korean' },
  { code: 'ar', label: 'Arabic' },
  { code: 'ru', label: 'Russian' },
  { code: 'hi', label: 'Hindi' },
]

// Regional variant / dialect choices per language — mirrors app.py
// LANGUAGE_VARIANTS. value is what's sent to the backend as `language_variant`.
const _v = (...pairs) => pairs.map(([label, value]) => ({ label, value }))
export const LANGUAGE_VARIANTS = {
  en: _v(
    ['🌍 Auto (General English)', 'General English'],
    ['🇺🇸 American English', 'American English (en-US)'],
    ['🇬🇧 British English', 'British English (en-GB)'],
    ['🇦🇺 Australian English', 'Australian English (en-AU)'],
    ['🇨🇦 Canadian English', 'Canadian English (en-CA)'],
    ['🇮🇪 Irish English', 'Irish English (en-IE)'],
    ['🇿🇦 South African English', 'South African English (en-ZA)'],
    ['🇮🇳 Indian English', 'Indian English (en-IN)'],
    ['🇸🇬 Singaporean English', 'Singaporean English (en-SG)'],
    ['🇵🇭 Filipino English', 'Filipino English (en-PH)'],
  ),
  es: _v(
    ['🌎 Auto (General Spanish)', 'General Spanish'],
    ['🇲🇽 Mexican Spanish', 'Mexican Spanish (es-MX)'],
    ['🇪🇸 Castilian Spanish (Spain)', 'Castilian Spanish (es-ES)'],
    ['🇨🇴 Colombian Spanish', 'Colombian Spanish (es-CO)'],
    ['🇦🇷 Argentinian Spanish', 'Argentinian Spanish (es-AR)'],
    ['🇨🇱 Chilean Spanish', 'Chilean Spanish (es-CL)'],
    ['🇺🇸 US Latino Spanish', 'US Latino Spanish (es-US)'],
  ),
  fr: _v(
    ['🌍 Auto (General French)', 'General French'],
    ['🇫🇷 France French', 'France French (fr-FR)'],
    ['🇨🇦 Canadian French (Québécois)', 'Canadian French (fr-CA)'],
    ['🇧🇪 Belgian French', 'Belgian French (fr-BE)'],
    ['🇨🇭 Swiss French', 'Swiss French (fr-CH)'],
  ),
  de: _v(
    ['🌍 Auto (General German)', 'General German'],
    ['🇩🇪 Standard German (Germany)', 'Standard German (de-DE)'],
    ['🇦🇹 Austrian German', 'Austrian German (de-AT)'],
    ['🇨🇭 Swiss German', 'Swiss German (de-CH)'],
    ['🌍 Bavarian dialect', 'Bavarian German'],
  ),
  it: _v(
    ['🌍 Auto (General Italian)', 'General Italian'],
    ['🇮🇹 Standard Italian', 'Standard Italian (it-IT)'],
    ['🇨🇭 Swiss Italian', 'Swiss Italian (it-CH)'],
    ['🌍 Sicilian', 'Sicilian Italian'],
    ['🌍 Neapolitan', 'Neapolitan Italian'],
  ),
  pt: _v(
    ['🌍 Auto (General Portuguese)', 'General Portuguese'],
    ['🇧🇷 Brazilian Portuguese', 'Brazilian Portuguese (pt-BR)'],
    ['🇵🇹 European Portuguese', 'European Portuguese (pt-PT)'],
    ['🇦🇴 Angolan Portuguese', 'Angolan Portuguese (pt-AO)'],
  ),
  zh: _v(
    ['🌍 Auto (General Chinese)', 'General Chinese'],
    ['🇨🇳 Mandarin Simplified (Mainland)', 'Mainland Mandarin (zh-CN)'],
    ['🇹🇼 Mandarin Traditional (Taiwan)', 'Taiwan Mandarin (zh-TW)'],
    ['🇭🇰 Cantonese (Hong Kong)', 'Cantonese (zh-HK)'],
    ['🇸🇬 Singaporean Mandarin', 'Singaporean Mandarin (zh-SG)'],
  ),
  ja: _v(
    ['🌍 Auto (General Japanese)', 'General Japanese'],
    ['🇯🇵 Standard Japanese (Tokyo)', 'Standard Japanese (Tokyo)'],
    ['🌍 Kansai / Osaka dialect', 'Kansai dialect'],
    ['🌍 Kyushu dialect', 'Kyushu dialect'],
  ),
  ko: _v(
    ['🌍 Auto (General Korean)', 'General Korean'],
    ['🇰🇷 Standard Korean (Seoul)', 'Standard Korean (Seoul)'],
    ['🌍 Gyeongsang dialect', 'Gyeongsang dialect'],
    ['🌍 Jeolla dialect', 'Jeolla dialect'],
  ),
  ar: _v(
    ['🌍 Auto / Modern Standard Arabic', 'Modern Standard Arabic'],
    ['🇪🇬 Egyptian Arabic', 'Egyptian Arabic'],
    ['🇸🇦 Saudi Arabic', 'Saudi Arabic'],
    ['🇦🇪 Gulf Arabic', 'Gulf Arabic'],
    ['🇱🇧 Levantine Arabic', 'Levantine Arabic'],
    ['🇲🇦 Moroccan Arabic (Darija)', 'Moroccan Arabic'],
  ),
  ru: _v(
    ['🌍 Auto (General Russian)', 'General Russian'],
    ['🇷🇺 Standard Russian (Moscow)', 'Standard Russian'],
    ['🌍 St. Petersburg Russian', 'St. Petersburg Russian'],
    ['🌍 Siberian Russian', 'Siberian Russian'],
  ),
  hi: _v(
    ['🔍 Auto (General Hindi)', 'General Hindi'],
    ['🇮🇳 Standard Hindi (Delhi)', 'Standard Hindi (Delhi)'],
    ['🇮🇳 Mumbai Hindi', 'Mumbai Hindi'],
    ['🇮🇳 Bihari Hindi', 'Bihari Hindi'],
    ['🇮🇳 Rajasthani Hindi', 'Rajasthani Hindi'],
  ),
}

// Output language for the report (PDF & DOCX) — used by the Regenerate control.
// "Same as source" skips translation. Mirrors app.py _PDF_LANGUAGES.
export const OUTPUT_LANGUAGES = [
  'Same as source', 'English', 'Spanish', 'French', 'German', 'Portuguese',
  'Italian', 'Dutch', 'Russian', 'Chinese (Simplified)', 'Japanese', 'Korean',
  'Arabic', 'Hindi', 'Turkish',
]

// What the app supports — shown in the "What we support" panel (mirrors _CAPABILITIES).
export const CAPABILITIES = [
  { icon: '🎵', title: 'Audio', text: 'mp3 · wav · m4a · flac · ogg · aac' },
  { icon: '🎬', title: 'Video', text: 'mp4 · mov · mkv · webm — auto audio extraction' },
  { icon: '📄', title: 'Documents', text: 'pdf · docx · txt · srt · vtt' },
  { icon: '🗣', title: 'Diarization', text: 'Multi-speaker separation (Panel mode)' },
  { icon: '🎤', title: 'Interview coaching', text: 'Per-question scoring, model answers, advancement %' },
  { icon: '🎥', title: 'Delivery analysis', text: 'Body language, emotion, eye contact + annotated video' },
  { icon: '🌐', title: '37+ languages', text: 'Auto-detect with regional dialects' },
  { icon: '🔒', title: '100% private', text: 'Local Whisper option — nothing leaves your machine' },
]
