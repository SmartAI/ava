import React from 'react'

import { iconButton, sidebarRow, statusDot } from '../ui'
import { ArchiveIcon, CaretIcon, ComposeIcon, FolderIcon, PlusIcon, SettingsIcon } from './Icons'

function ChatRow({ chat, current, status, onSelect, onArchive }) {
  const label = chat.archived ? 'Unarchive chat' : 'Archive chat'
  const dotState = current && status !== 'idle' ? status : ''
  return <div className={`group flex items-center rounded-lg ${current ? 'bg-selected' : 'hover:bg-hover'}`}>
    <button className={`${sidebarRow} min-w-0 flex-1 pl-8 text-[13px] ${current ? 'text-ink' : ''}`} onClick={onSelect}>
      <span className={statusDot(dotState)} aria-hidden="true" />
      <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{chat.title || 'New chat'}</span>
    </button>
    <button className={`${iconButton} opacity-0 group-hover:opacity-100 focus-visible:opacity-100`} type="button" aria-label={label} title={label} onClick={onArchive}><ArchiveIcon /></button>
  </div>
}

export function Sidebar({ projects, current, status, selection, open, archiveOpen, mobileOpen, onClose, onAddProject, onNew, onNewSession, onSettings, onToggleProject, onToggleArchive, onSelect, onArchive }) {
  return <>
    <aside className={`fixed inset-y-0 left-0 z-30 flex w-[min(84vw,320px)] shrink-0 flex-col border-r border-line bg-sidebar shadow-float transition-transform duration-150 motion-reduce:transition-none min-[701px]:static min-[701px]:z-auto min-[701px]:w-[252px] min-[701px]:translate-x-0 min-[701px]:shadow-none ${mobileOpen ? 'translate-x-0' : '-translate-x-[102%]'}`}>
      <div className="flex h-13 shrink-0 items-center gap-2 py-0 pr-3 pl-4">
        <span className="flex-1 text-[15px] font-semibold tracking-[0.01em]">ava</span>
        <button className={iconButton} type="button" title="Add project" aria-label="Add project" onClick={onAddProject}><PlusIcon /></button>
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" aria-label="Projects and chats">
        <button className={`${sidebarRow} mt-1 font-medium text-ink`} onClick={onNew}>
          <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><ComposeIcon /></span>
          <span className="min-w-0 flex-1">New chat</span>
        </button>
        <div className="px-2 pt-3.5 pb-1 text-xs font-medium text-faint">Projects</div>
        {projects.map(project => {
          const expanded = open.has(project.id)
          const archived = project.chats.filter(chat => chat.archived)
          const archivedExpanded = archiveOpen.has(project.id)
          return <section key={project.id}>
            <div className="group flex items-center rounded-lg hover:bg-hover">
              <button className={`${sidebarRow} min-w-0 flex-1 text-ink`} type="button" title={project.path} aria-expanded={expanded} onClick={() => onToggleProject(project.id)}>
                <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><CaretIcon className={`transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} /></span>
                <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><FolderIcon /></span>
                <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{project.name}</span>
              </button>
              <button className={`${iconButton} mr-0.5 text-faint`} type="button" title="New session" aria-label={`New session in ${project.name}`} onClick={() => onNewSession(project.id)}><PlusIcon /></button>
            </div>
            {expanded && <div>
              {project.chats.filter(chat => !chat.archived).map(chat => <ChatRow key={chat.id} chat={chat} current={chat.id === current} status={status} onSelect={() => onSelect(chat.id)} onArchive={() => onArchive(chat, true)} />)}
              {archived.length > 0 && <div>
                <button className={`${sidebarRow} pl-8 text-xs text-faint`} aria-expanded={archivedExpanded} onClick={() => onToggleArchive(project.id)}>
                  <span className="min-w-0 flex-1">Archived</span>
                  <CaretIcon className={`transition-transform duration-150 ${archivedExpanded ? 'rotate-90' : ''}`} />
                </button>
                {archivedExpanded && archived.map(chat => <ChatRow key={chat.id} chat={chat} current={chat.id === current} status={status} onSelect={() => onSelect(chat.id)} onArchive={() => onArchive(chat, false)} />)}
              </div>}
            </div>}
          </section>
        })}
      </nav>
      <div className="shrink-0 border-t border-line px-2 py-2">
        <button className={`${sidebarRow} py-2`} type="button" onClick={onSettings}>
          <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center text-quiet"><SettingsIcon /></span>
          <span className="min-w-0 flex-1">
            <span className="block text-ink">Settings</span>
            <span className="block overflow-hidden text-ellipsis whitespace-nowrap text-[11px] text-faint">
              Provider connections & appearance
            </span>
          </span>
        </button>
      </div>
    </aside>
    {mobileOpen && <button className="fixed inset-y-0 right-0 left-[min(84vw,320px)] z-20 border-0 bg-mask min-[701px]:hidden" aria-label="Close navigation" onClick={onClose} />}
  </>
}
