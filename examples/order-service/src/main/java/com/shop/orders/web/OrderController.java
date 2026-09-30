package com.shop.orders.web;

import com.shop.orders.model.Order;
import com.shop.orders.service.OrderService;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService orders;

    public OrderController(OrderService orders) {
        this.orders = orders;
    }

    @GetMapping("/{orderId}")
    public Order getOrder(@PathVariable Long orderId) {
        return orders.find(orderId);
    }

    @GetMapping
    public List<Order> listOrders(@RequestParam(required = false) Long customerId) {
        return orders.list(customerId);
    }

    @PostMapping
    public Order createOrder(@RequestBody Order order) {
        return orders.save(order);
    }
}
