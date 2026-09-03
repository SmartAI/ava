import React from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'

const storedTheme = localStorage.getItem('ava-theme')
if (storedTheme) document.documentElement.dataset.theme = storedTheme

createRoot(document.getElementById('root')).render(<App />)
