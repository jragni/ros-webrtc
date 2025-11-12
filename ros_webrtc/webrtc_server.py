#!/usr/bin/env python3
"""
Simple WebRTC video streaming server using aiortc.
Subscribes to ROS2 image topic and streams via WebRTC.
"""

import asyncio
import json
import logging
import traceback
from typing import Optional

from aiohttp import web
import aiohttp_cors
from av import VideoFrame
import cv2
import numpy as np

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCConfiguration, RTCIceServer

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ROS2VideoTrack(VideoStreamTrack):
    """
    Video track that gets frames from ROS2 WebRTC server node.
    Each peer connection gets its own track instance.
    """

    def __init__(self, server_node, width: int = 640, height: int = 480):
        super().__init__()
        self.server_node = server_node  # Reference to WebRTCServer node
        self.width = width
        self.height = height
        self._frame_send_count = 0

    async def recv(self) -> VideoFrame:
        """Get next video frame for WebRTC."""
        pts, time_base = await self.next_timestamp()

        # Get frame from server node (shared across all tracks)
        async with self.server_node.frame_lock:
            if self.server_node.latest_frame is None:
                # Return black frame if no image yet
                logger.warning("recv() called but no frame available yet, sending black frame")
                frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            else:
                frame = self.server_node.latest_frame.copy()
                # Log first frame and every 30th frame
                self._frame_send_count += 1
                if self._frame_send_count == 1 or self._frame_send_count % 30 == 0:
                    logger.info(f"Track {id(self)}: Sending frame {self._frame_send_count} to WebRTC client")

        # Convert BGR to RGB (OpenCV uses BGR, WebRTC expects RGB)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Create VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base

        return video_frame


class WebRTCServer(Node):
    """
    ROS2 node that runs WebRTC signaling server and streams video.
    """

    def __init__(self):
        super().__init__('webrtc_server')

        # Parameters
        self.declare_parameter('image_topic', '/image_raw')
        self.declare_parameter('port', 8080)
        self.declare_parameter('image_width', 640)
        self.declare_parameter('image_height', 480)

        self.image_topic = self.get_parameter('image_topic').value
        self.port = self.get_parameter('port').value
        self.image_width = self.get_parameter('image_width').value
        self.image_height = self.get_parameter('image_height').value

        # CV Bridge
        self.bridge = CvBridge()

        # Latest frame storage (shared across all video tracks)
        self.latest_frame: Optional[np.ndarray] = None
        self.frame_lock = asyncio.Lock()

        # Subscribe to image topic
        self.subscription = self.create_subscription(
            Image,
            self.image_topic,
            self.image_callback,
            10
        )

        # Peer connections
        self.pcs: set[RTCPeerConnection] = set()

        # Track background tasks to prevent them from being garbage collected
        self.background_tasks: set[asyncio.Task] = set()

        # Frame counter for debug logging
        self.frame_count = 0

        self.get_logger().info(f'WebRTC server starting on port {self.port}')
        self.get_logger().info(f'Subscribing to: {self.image_topic}')

    async def set_latest_frame(self, frame: np.ndarray) -> None:
        """Update the latest frame (called from ROS callback)."""
        async with self.frame_lock:
            self.latest_frame = frame

    def image_callback(self, msg: Image) -> None:
        """Handle incoming ROS2 images."""
        try:
            # Convert ROS image to OpenCV
            cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')

            # Store frame (shared across all peer connections)
            task = asyncio.create_task(self.set_latest_frame(cv_image))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)

            # Log first frame and every 30th frame to confirm receiving images
            self.frame_count += 1
            if self.frame_count == 1 or self.frame_count % 30 == 0:
                self.get_logger().info(f'Received frame {self.frame_count}, size: {cv_image.shape}')

        except Exception as e:
            self.get_logger().error(f'Error processing image: {e}')


# Global node instance (simplified for aiohttp integration)
ros_node: Optional[WebRTCServer] = None


async def offer(request: web.Request) -> web.Response:
    """Handle WebRTC offer from client."""
    try:
        # Parse request
        params = await request.json()

        if "sdp" not in params or "type" not in params:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Missing 'sdp' or 'type' in request"})
            )

        logger.info(f"Received offer from client, type: {params['type']}")

        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        # Create peer connection with ICE servers for tunnel compatibility
        ice_servers = [
            RTCIceServer(urls="stun:stun.l.google.com:19302"),
            RTCIceServer(urls="stun:stun1.l.google.com:19302"),
        ]
        configuration = RTCConfiguration(iceServers=ice_servers)
        pc = RTCPeerConnection(configuration=configuration)
        ros_node.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                ros_node.pcs.discard(pc)

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state: {pc.iceConnectionState}")

        # IMPORTANT: Set remote description FIRST, then add track
        # This ensures m-line order matches between offer and answer
        logger.info("Setting remote description...")
        await pc.setRemoteDescription(offer)
        logger.info("Remote description set successfully")

        # Create a fresh video track for this peer connection
        # This prevents issues when multiple connections are created/destroyed
        video_track = ROS2VideoTrack(
            server_node=ros_node,
            width=ros_node.image_width,
            height=ros_node.image_height
        )
        pc.addTrack(video_track)
        logger.info(f"Added fresh video track (id={id(video_track)}) to peer connection")

        # Create answer
        logger.info("Creating answer...")
        answer = await pc.createAnswer()
        logger.info("Setting local description...")
        await pc.setLocalDescription(answer)
        logger.info("Local description set successfully")

        # Ensure type is a string (aiortc might use enum)
        response_data = {
            "sdp": pc.localDescription.sdp,
            "type": str(pc.localDescription.type)
        }

        logger.info(f"Sending answer back to client, type: {response_data['type']}")

        return web.Response(
            content_type="application/json",
            text=json.dumps(response_data)
        )

    except json.JSONDecodeError:
        return web.Response(
            status=400,
            content_type="application/json",
            text=json.dumps({"error": "Invalid JSON"})
        )
    except Exception as e:
        logger.error(f"Error handling offer: {e}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")

        # Clean up peer connection on error
        if 'pc' in locals() and pc in ros_node.pcs:
            ros_node.pcs.discard(pc)
            await pc.close()

        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )


async def health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"status": "ok", "service": "webrtc-server"})
    )


async def on_shutdown(_app: web.Application) -> None:
    """Close all peer connections on shutdown."""
    coros = [pc.close() for pc in ros_node.pcs]
    await asyncio.gather(*coros, return_exceptions=True)
    ros_node.pcs.clear()


def main(args=None):
    """Main entry point."""
    global ros_node

    # Initialize ROS2
    rclpy.init(args=args)
    ros_node = WebRTCServer()

    # Create aiohttp app
    app = web.Application()

    # Configure CORS - Allow all origins for tunnel compatibility
    # This allows ngrok, localtunnel, Cloudflare tunnels, etc.
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "HEAD", "OPTIONS"]
        )
    })


    # Add routes and enable CORS
    health_route = app.router.add_get("/", health)
    cors.add(health_route)

    offer_route = app.router.add_post("/offer", offer)
    cors.add(offer_route)

    app.on_shutdown.append(on_shutdown)

    # Run both ROS2 and web server
    async def run_all():
        # Start web server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", ros_node.port)
        await site.start()

        logger.info(f"WebRTC server running on http://0.0.0.0:{ros_node.port}")

        # Spin ROS2 node
        while rclpy.ok():
            rclpy.spin_once(ros_node, timeout_sec=0.01)
            await asyncio.sleep(0.01)

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        pass
    finally:
        ros_node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
