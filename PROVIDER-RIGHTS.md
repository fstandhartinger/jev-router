# Provider-rights launch gate

Checked 21 September 2026. This is a conservative product-routing decision, not legal advice.

## TypeSafe original Jev

Source: [TypeSafe Master Customer Agreement](https://typesafe.ai/legal/mca), sections 2.2 and 2.3.

Section 2.2 permits integration into customer-operated applications for their end users. Section 2.3 prohibits offering the service as a standalone service and using it to build a similar or competing service. Jev Router republishes a generic decision interface across interchangeable models, so it is materially closer to the prohibited standalone case than to a vertical application.

Launch decision: disabled. We do not use our TypeSafe key for customers and do not proxy customer keys. Direct provider use remains available outside Jev Router. Written TypeSafe permission is required before enabling it.

## Vercel AI Gateway Jev

Sources: [Vercel AI Product Terms](https://vercel.com/legal/ai-product-terms) and [Vercel Jev model page](https://vercel.com/ai-gateway/models/jev).

The AI Gateway terms contemplate applications and end users, but also require compliance with the underlying model provider's terms. They therefore do not cure TypeSafe's standalone-service restriction; the terms also contain internal-use language that makes resale through our key insufficiently clear.

Launch decision: disabled. We do not use our Vercel key for customers and do not custody/proxy customer Vercel keys for Jev. Written permission from both relevant providers is required.

## Maisa hosted djev

Source: [djev developer documentation](https://djev.dev/).

The documentation explains server-side application integration, but no public commercial proxy/resale grant was found.

Launch decision: Maisa's hosted API is not proxied. The independently self-hosted Apache-2.0 `Davipar/djev-dev` runtime over Google's Apache-2.0 DiffusionGemma weights is the only djev path offered.

## Pricing policy

Third-party pass-through routes, if later permitted, will have zero Jev Router markup. Only self-hosted models carry the small infrastructure margin disclosed per 1,000 decisions on `/models`.
