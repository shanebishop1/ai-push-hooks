import { api } from "../api/client";

type Invoice = { id: string; orderId: string; issuedAt: string };

export async function loadInvoice(invoiceId: string): Promise<Invoice> {
  return api.get<Invoice>(`/invoices/${invoiceId}`);
}
