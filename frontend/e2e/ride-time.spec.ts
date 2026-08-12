import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Regression/behaviour test: the displayed time in the planner's stats row
// is the rider-adjusted ride-time estimate, computed on read from the
// user's settings - not the raw routing duration. Halving the rider's
// flat-road speed must make the same route show a longer time.

const email = `e2e-ridetime-${Date.now()}@example.com`;
const password = 'e2e-ridetime-password-1';

function parseDurationMinutes(text: string): number {
	const hoursMatch = text.match(/(\d+)\s*h/);
	const minsMatch = text.match(/(\d+)\s*min/);
	const hours = hoursMatch ? Number(hoursMatch[1]) : 0;
	const mins = minsMatch ? Number(minsMatch[1]) : 0;
	return hours * 60 + mins;
}

async function planASampleRoute(page: import('@playwright/test').Page) {
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	// Let the map settle before clicking waypoints onto it.
	await page.waitForTimeout(2500);

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 40, cy - 30);
	await page.mouse.click(cx + 40, cy + 30);

	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});
}

test('halving the rider flat speed increases the displayed ride time', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	await planASampleRoute(page);

	const durationSpan = page.locator('.stats span').nth(1);
	await expect(durationSpan).toBeVisible();
	const before = parseDurationMinutes((await durationSpan.textContent()) ?? '');

	await page.goto('/settings');
	const speedInput = page.locator('input[type="number"]').nth(1);
	await expect(speedInput).toBeVisible();
	const currentSpeed = Number(await speedInput.inputValue());
	await speedInput.fill(String(currentSpeed / 2));
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await expect(page.getByText('Saved')).toBeVisible();

	await page.goto('/');
	await planASampleRoute(page);

	const afterDurationSpan = page.locator('.stats span').nth(1);
	await expect(afterDurationSpan).toBeVisible();
	const after = parseDurationMinutes((await afterDurationSpan.textContent()) ?? '');

	expect(after).toBeGreaterThan(before);
});
