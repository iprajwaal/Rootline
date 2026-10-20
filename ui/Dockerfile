FROM node:22-alpine AS build

RUN apk add --no-cache git && corepack enable

WORKDIR /app

# Pinned so the UI build doesn't change under us.
ARG CHAT_UI_COMMIT=99304ec893ee380696b37077439f514c49487584
RUN git clone https://github.com/langchain-ai/agent-chat-ui.git . \
    && git checkout "$CHAT_UI_COMMIT"

# Our changes to the upstream UI, kept as patches so the fork stays small and readable.
COPY patches/ /patches/
RUN git apply /patches/*.patch

# Next.js bakes NEXT_PUBLIC_* values into the bundle at build time.
ARG NEXT_PUBLIC_API_URL=http://localhost:2024
ARG NEXT_PUBLIC_ASSISTANT_ID=agent
ARG NEXT_PUBLIC_ROOTLINE_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_ASSISTANT_ID=$NEXT_PUBLIC_ASSISTANT_ID \
    NEXT_PUBLIC_ROOTLINE_API_URL=$NEXT_PUBLIC_ROOTLINE_API_URL

RUN pnpm install --frozen-lockfile && pnpm build

FROM node:22-alpine

RUN corepack enable

WORKDIR /app
COPY --from=build /app ./

EXPOSE 3000
CMD ["pnpm", "start"]
