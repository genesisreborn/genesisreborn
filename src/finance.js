// Pure loan maths. All money is integer cents.

/**
 * Build the full repayment schedule.
 * mode 'reducing': standard amortisation, interest on outstanding balance.
 * mode 'flat':     interest = principal * rate * years, spread evenly.
 */
export function buildSchedule({ principalCents, annualRate, months, mode = 'reducing' }) {
  const rows = [];
  if (mode === 'flat') {
    const totalInterest = Math.round((principalCents * annualRate * months) / 1200);
    const total = principalCents + totalInterest;
    const basePay = Math.floor(total / months);
    const baseInt = Math.round(totalInterest / months);
    let balance = principalCents;
    for (let n = 1; n <= months; n++) {
      const last = n === months;
      const payment = last ? total - basePay * (months - 1) : basePay;
      const interest = last ? totalInterest - baseInt * (months - 1) : baseInt;
      const principal = payment - interest;
      balance -= principal;
      rows.push({ n, payment, interest, principal, balance });
    }
    return rows;
  }

  const r = annualRate / 1200;
  const pmt = r === 0
    ? Math.round(principalCents / months)
    : Math.round((principalCents * r) / (1 - Math.pow(1 + r, -months)));
  let balance = principalCents;
  for (let n = 1; n <= months; n++) {
    const interest = Math.round(balance * r);
    const last = n === months;
    const principal = last ? balance : pmt - interest;
    const payment = principal + interest;
    balance -= principal;
    rows.push({ n, payment, interest, principal, balance });
  }
  return rows;
}

const sum = (rows, key) => rows.reduce((a, r) => a + r[key], 0);

export function summarize(rows, paidCount) {
  const paid = rows.slice(0, paidCount);
  const left = rows.slice(paidCount);
  return {
    totalPayable: sum(rows, 'payment'),
    totalInterest: sum(rows, 'interest'),
    paidPayments: sum(paid, 'payment'),
    paidInterest: sum(paid, 'interest'),
    remainingPayments: sum(left, 'payment'),
    remainingInterest: sum(left, 'interest'),
    remainingPrincipal: paidCount === 0 ? sum(rows, 'principal') : (paid[paidCount - 1]?.balance ?? 0),
    next: left[0] ?? null,
    done: left.length === 0,
  };
}

// ---- dates (year-month index = year*12 + month0) ----

export function dueDate(startYm, dueDay, n) {
  const ym = startYm + (n - 1);
  const y = Math.floor(ym / 12);
  const m = ym % 12;
  const dim = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  return { y, m: m + 1, d: Math.min(dueDay, dim) };
}

export const ymd = ({ y, m, d }) => `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

export function daysBetween(a, b) {
  return Math.round((Date.UTC(b.y, b.m - 1, b.d) - Date.UTC(a.y, a.m - 1, a.d)) / 86400000);
}

/** start_ym of installment #1 given that `paid` installments are already done and the next one falls on/after today. */
export function startYmFor(today, dueDay, paid) {
  let ym = today.y * 12 + (today.m - 1);
  const dim = new Date(Date.UTC(today.y, today.m, 0)).getUTCDate();
  if (Math.min(dueDay, dim) < today.d) ym += 1; // this month's due date already passed
  return ym - paid;
}

export function parseMoney(s) {
  if (!/^\d+([.,]\d{1,2})?$/.test(s ?? '')) return null;
  const cents = Math.round(parseFloat(s.replace(',', '.')) * 100);
  return cents > 0 ? cents : null;
}
