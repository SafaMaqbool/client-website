import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  redirects: {
    '/leistungen': '/leistungen/generalunternehmer',
  },
  vite: {
    plugins: [tailwindcss()],
  },
});
