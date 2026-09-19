import { api } from "../api/client";

type Order = { id: string; total: number; status: string };

export async function loadOrder(orderId: string): Promise<Order> {
  return api.get<Order>(`/orders/${orderId}`);
}
