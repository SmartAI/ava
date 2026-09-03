const call = async (method, path, body) => {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.json().then(payload => payload.error).catch(() => '')
    throw new Error(detail || `${method} ${path} returned ${response.status}`)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  projects: () => call('GET', '/api/projects').then(payload => payload.projects),
  addProject: path => call('POST', '/api/projects', { path }),
  browse: path => call('GET', `/api/fs?path=${encodeURIComponent(path || '')}`),
  createChat: project => call('POST', '/api/chats', { project_id: project }),
  open: id => call('GET', `/api/chats/${id}`),
  message: (id, text, attachments, delivery) =>
    call('POST', `/api/chats/${id}/messages`, { text, attachments, delivery }),
  archive: (id, archived) => call('POST', `/api/chats/${id}/archive`, { archived }),
  cancel: (id, cause) => call('POST', `/api/chats/${id}/cancel`, { cause }),
  resume: id => call('POST', `/api/chats/${id}/resume`),
  models: id => call('GET', `/api/chats/${id}/models`),
  selectModel: (id, body) => call('POST', `/api/chats/${id}/model`, body),
  compact: id => call('POST', `/api/chats/${id}/compact`),
  context: id => call('GET', `/api/chats/${id}/context`),
  skills: id => call('GET', `/api/chats/${id}/skills`).then(payload => payload.skills),
  login: (provider, key) => call('POST', '/api/credentials', { provider, key }),
  logout: provider => call('DELETE', `/api/credentials/${encodeURIComponent(provider)}`),
}
