import { defineConfig } from '@playwright/test';

// The service worker and web manifest are production-build artifacts - they do
// not exist under `vite dev`. So the PWA suite runs against a real built app
// served by `vite preview`, in its own testDir, kept out of the dev-server
// smoke suite (playwright.config.ts) where it would pass for the wrong reason.
//
//   npm run e2e:pwa
//
// needs no running compose stack: /api is absent under preview, which is
// exactly what the "API never cached" assertion wants to see.
const PORT = 4319;

export default defineConfig({
	testDir: 'e2e-pwa',
	timeout: 60_000,
	projects: [
		{ name: 'chromium', use: { browserName: 'chromium' } },
		{
			name: 'webkit-ios-layout',
			// WebKit is the iPhone layout engine. Keep service-worker lifecycle
			// coverage in Chromium, where Playwright supports the offline reload,
			// and run the complete mocked UI/state surface here with workers
			// unregistered so page.route() remains authoritative after reloads.
			grep: /mobile PWA|desktop breakpoint|desktop navigation and ordinary-page scrolling/,
			use: { browserName: 'webkit', serviceWorkers: 'block' }
		}
	],
	use: {
		baseURL: process.env.PWA_BASE_URL ?? `http://localhost:${PORT}`
	},
	webServer: process.env.PWA_BASE_URL
		? undefined
		: {
				command: `npm run build && npm run preview -- --port ${PORT} --strictPort`,
				url: `http://localhost:${PORT}`,
				reuseExistingServer: !process.env.CI,
				timeout: 180_000
			}
});
