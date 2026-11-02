CREATE TABLE products (
	id SERIAL NOT NULL,
	sku VARCHAR(40) NOT NULL,
	name VARCHAR(120) NOT NULL,
	category VARCHAR(80) NOT NULL,
	capacity INTEGER NOT NULL,
	daily_rate_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (capacity >= 0),
	CHECK (daily_rate_cents >= 0),
	UNIQUE (sku)
);

CREATE TABLE bookings (
	id VARCHAR(36) NOT NULL,
	reference VARCHAR(20) NOT NULL,
	label VARCHAR(120) NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(20) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_by VARCHAR(80) NOT NULL,
	created_by_name VARCHAR(80) NOT NULL,
	total_cents BIGINT NOT NULL,
	PRIMARY KEY (id),
	CHECK (end_at > start_at),
	CHECK (total_cents >= 0 AND total_cents <= 9007199254740991),
	CHECK (status IN ('hold','confirmed','checked_out','returned','cancelled','expired')),
	UNIQUE (reference)
);

CREATE TABLE booking_items (
	booking_id VARCHAR(36) NOT NULL,
	product_id INTEGER NOT NULL,
	quantity INTEGER NOT NULL,
	rate_cents BIGINT NOT NULL,
	PRIMARY KEY (booking_id, product_id),
	CHECK (quantity > 0),
	CHECK (rate_cents >= 0),
	FOREIGN KEY(booking_id) REFERENCES bookings (id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);
