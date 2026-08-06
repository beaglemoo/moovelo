import { defineConfig } from '@playwright/test';

// Smoke tests run against the dev compose stack (vite on 5173 proxying the
// backend). They need Valhalla routing tiles built, so they are not part of
// the default CI pipeline - run locally with `npx playwright test`.
export default defineConfig({
	testDir: 'e2e',
	timeout: 60_000,
	use: {
		baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173'
	}
});
