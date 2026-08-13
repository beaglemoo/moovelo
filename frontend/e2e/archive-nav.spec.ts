import { expect, test } from '@playwright/test';
import { registerOrSkip, windowDropReady } from './support/auth';

// Navigating off /activities and back while an archive import is genuinely
// still running used to lose the card entirely: the attempt/Poller state was
// component-local $state, destroyed on unmount and never reconciled on the
// next mount, so a real in-flight import showed no card, no progress, and a
// re-enabled Import button - inviting a second upload on top of the first.

const password = 'e2e-archive-nav-password-1';

function runningJob(id: string, filename: string) {
	return {
		id,
		filename,
		status: 'running' as const,
		total: 500,
		imported: 3,
		failed: 0,
		skipped: 0,
		duplicates: 0,
		error: null,
		problems: []
	};
}

async function dropZip(page: import('@playwright/test').Page, name: string) {
	await page.evaluate((filename) => {
		const eocd = new Uint8Array(22);
		eocd.set([0x50, 0x4b, 0x05, 0x06]);
		const transfer = new DataTransfer();
		transfer.items.add(new File([eocd], filename, { type: 'application/zip' }));
		window.dispatchEvent(
			new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true })
		);
	}, name);
}

test('a still-running archive import stays visible after leaving /activities and coming back', async ({
	page
}) => {
	await registerOrSkip(page, 'e2e-archive-nav', password);

	// A job that never finishes for the duration of the test.
	await page.route('**/api/activities/import/archive', (route) =>
		route.fulfill({ json: runningJob('job-nav', 'archive-nav.zip') })
	);
	await page.route('**/api/activities/import/archive/job-nav', (route) =>
		route.fulfill({ json: runningJob('job-nav', 'archive-nav.zip') })
	);

	await page.goto('/activities');
	await windowDropReady(page);

	await dropZip(page, 'archive-nav.zip');
	await expect(page.locator('.archive')).toContainText('archive-nav.zip', { timeout: 10_000 });
	const importButton = page.getByRole('button', { name: /Import rides|Importing/ });
	await expect(importButton).toBeDisabled();

	// Leave and come back via client-side routing while the import runs on.
	await page.getByRole('link', { name: 'Planner' }).click();
	await expect(page).toHaveURL(/\/$/);
	await page.getByRole('link', { name: 'Activities' }).click();
	await expect(page).toHaveURL(/\/activities$/);

	// The import is still running server-side, so the card must still be here
	// and the button still disabled - never a re-enabled button with no card.
	await expect(page.locator('.archive')).toContainText('archive-nav.zip', { timeout: 10_000 });
	await expect(page.getByRole('button', { name: /Import rides|Importing/ })).toBeDisabled();
});
