import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

/** Independent build only; no ordinary app entry, public asset copy or dev server. */
export default defineConfig({
  base:'./',
  publicDir:false,
  plugins:[react(),tailwindcss()],
  resolve:{alias:{'@':path.resolve(__dirname,'./src')}},
  build:{
    outDir:'dist-manager',
    sourcemap:false,
    chunkSizeWarningLimit:900,
    rollupOptions:{input:path.resolve(__dirname,'manager.html')},
  },
})
