/** The one place that knows whether a spec can register a user.
 *
 * Every spec that needs a logged-in browser registers one over the API, and
 * each of them used to inline the same precondition:
 *
 *     status.setup_required || (status.signups_enabled && status.password_login)
 *
 * which is wrong, and wrong in a way that fails LOUDLY rather than skipping.
 * `POST /api/auth/register` refuses with 403 whenever password login is off,
 * *regardless* of whether a first user exists (backend/app/api/auth.py) - so
 * on a stack with no users and SSO-only login the expression reads true via
 * `setup_required`, the spec does not skip, and it dies on the register call
 * instead. That stack is not exotic: it is the project's own dev default
 * (PASSWORD_AUTH_ENABLED=false with OIDC configured) and the shape staging
 * and prod run.
 *
 * The rule is simply what the endpoint does: password login must be active,
 * AND this must either be the first user or an install with signups open.
 *
 * It lives here rather than in each spec because there were thirty identical
 * copies of the wrong version, and the thirty-first would have been wrong
 * too.
 */
import { expect, test, type Page } from '@playwright/test';

export interface AuthStatus {
	setup_required: boolean;
	signups_enabled: boolean;
	password_login: boolean;
}

export function canRegister(status: AuthStatus): boolean {
	return status.password_login && (status.setup_required || status.signups_enabled);
}

/** The reason shown when a spec skips, so every spec gives the same one.
 *
 * It names PASSWORD_AUTH_ENABLED because that is the flag people actually
 * have to change. The message this replaced said only "needs a fresh DB or
 * SIGNUPS_ENABLED=true", which sends a reader to set the one variable that
 * does not help: with password login off, the spec skips no matter what
 * SIGNUPS_ENABLED says. Same root cause as the predicate this file exists
 * for, so the same omission, in the sentence explaining it. */
export const NEEDS_REGISTRATION =
	'needs PASSWORD_AUTH_ENABLED=true, plus a fresh DB or SIGNUPS_ENABLED=true';

/** Skip unless this stack can register, then register a fresh user.
 *
 * The whole ritual - fetch status, skip, POST, assert - in the one place,
 * because the ritual is what kept getting copied. `prefix` only keeps
 * concurrent specs from colliding on an email; the timestamp does the real
 * work of making it unique.
 */
export async function registerOrSkip(page: Page, prefix: string, password: string): Promise<void> {
	const status = (await page.request.get('/api/auth/status').then((r) => r.json())) as AuthStatus;
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `${prefix}-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}
