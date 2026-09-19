type Refund = { id: string; amountCents: number };

// Issue a refund for an order.
export async function submitRefund(
  orderId: string,
  amountCents: number,
): Promise<Refund> {
  const response = await fetch(`https://api.internal/orders/${orderId}/refunds`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ amountCents }),
  });

  return (await response.json()) as Refund;
}
