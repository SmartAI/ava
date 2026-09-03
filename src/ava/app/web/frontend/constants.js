export const HINTS = {
  idle: 'Enter to send · Shift+Enter for a newline',
  running: 'Enter steers · Alt+Enter queues · Esc pauses',
  pausing: 'Enter steers on resume · Alt+Enter queues · Esc aborts',
  paused: 'Enter resumes (text steers) · Alt+Enter queues',
  aborting: 'Aborting…',
}

export const PLACEHOLDERS = {
  idle: 'Ask ava to do something…',
  running: 'Steer the current turn…',
  pausing: 'Steer the resumed turn…',
  paused: 'Continue, or steer the resumed turn…',
  aborting: 'Aborting…',
}

export const BUTTON_MODES = { running: 'pause', pausing: 'abort', paused: 'resume' }

export const BUTTON_TITLES = {
  send: 'Send',
  pause: 'Pause after the current step',
  abort: 'Abort now',
  resume: 'Resume',
}

export const STATUS_LABELS = {
  running: 'Working',
  pausing: 'Pausing · finishing the current step',
  aborting: 'Aborting',
}

export const COMMAND_HINTS = {
  model: '/model [ID]  choose the model; applies at the next step',
  effort: '/effort [LEVEL]  set reasoning effort; "none" clears it',
  compact: '/compact  summarize older history now',
  context: '/context  what the model sees now, by kind and size',
  skills: '/skills  list the skills the model can load',
  login: '/login [PROVIDER]  store an API key for a provider',
  logout: '/logout [PROVIDER]  remove a stored API key',
  theme: '/theme  toggle light and dark',
  copy: '/copy [code]  copy the last answer, or its last code block',
  new: '/new  start a fresh chat in this project',
  clear: '/clear  same as /new',
  pause: '/pause  stop after the current step',
  abort: '/abort  stop now and repair history',
  resume: '/resume  continue a paused turn',
  help: '/help  this list',
}
