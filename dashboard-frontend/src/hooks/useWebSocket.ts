import { useEffect, useRef, useState, useCallback } from 'react'
import type { MeshHealth, Alert, EnvEvent } from '@/lib/api'

interface WebSocketMessage {
  type: string
  data?: unknown
  event?: EnvEvent
}

interface UseWebSocketReturn {
  connected: boolean
  lastHealth: MeshHealth | null
  lastAlert: Alert | null
  lastMessage: WebSocketMessage | null
}

export function useWebSocket(): UseWebSocketReturn {
  const [connected, setConnected] = useState(false)
  const [lastHealth, setLastHealth] = useState<MeshHealth | null>(null)
  const [lastAlert, setLastAlert] = useState<Alert | null>(null)
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)
  const reconnectDelayRef = useRef(1000)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      return
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/live`

    try {
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        reconnectDelayRef.current = 1000 // Reset backoff on successful connection
      }

      ws.onmessage = (event) => {
        try {
          const message: WebSocketMessage = JSON.parse(event.data)

          // Store all messages for generic handling
          setLastMessage(message)

          switch (message.type) {
            case 'health_update':
              setLastHealth(message.data as MeshHealth)
              break
            case 'alert_fired':
              setLastAlert(message.data as Alert)
              break
            // env_update messages are handled via lastMessage
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e)
        }
      }

      ws.onclose = () => {
        setConnected(false)
        wsRef.current = null

        // Schedule reconnect with exponential backoff
        const delay = Math.min(reconnectDelayRef.current, 30000)
        reconnectTimeoutRef.current = window.setTimeout(() => {
          reconnectDelayRef.current = Math.min(delay * 2, 30000)
          connect()
        }, delay)
      }

      ws.onerror = () => {
        ws.close()
      }

      // Keepalive ping every 30 seconds
      const pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send('ping')
        }
      }, 30000)

      ws.addEventListener('close', () => {
        clearInterval(pingInterval)
      })
    } catch (e) {
      console.error('Failed to create WebSocket:', e)
    }
  }, [])

  useEffect(() => {
    connect()

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [connect])

  return { connected, lastHealth, lastAlert, lastMessage }
}
